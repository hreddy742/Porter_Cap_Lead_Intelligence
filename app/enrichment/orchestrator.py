"""
Contactability-Lite enrichment orchestrator.

run_contactability_enrichment: selects active lead candidates, enriches
each with provider stubs, optionally persists results.

dry_run=True (default): queries DB to count candidates; no writes, no HTTP.
apply=False:            forces dry_run=True regardless of the dry_run flag.
"""
from __future__ import annotations

import uuid as _uuid_module
from datetime import datetime, timezone
from uuid import UUID

import structlog
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.models import CompanyContactability, ContactabilityRun
from app.enrichment.providers.base import SAMResult, WebsiteContactResult, WebsiteResult
from app.enrichment.providers.sam_gov import SAMGovProvider
from app.enrichment.providers.search import BraveSearchProvider
from app.enrichment.status_logic import compute_contactability_score, compute_contactability_status
from app.enrichment.website_extractor import extract_website_contacts
from app.ops.sentry import capture_exception

logger = structlog.get_logger(__name__)

# A SAM lookup that returns one of these told us something real about the
# company (it matched, definitively did not match, or had no UEI to look up).
_SAM_USEFUL = frozenset({"matched", "not_found", "no_uei"})
# These mean the provider failed (throttle / API error). They are NOT evidence
# that the company is uncontactable and must never be stored as such.
_SAM_PROVIDER_FAILURE = frozenset({"rate_limited", "error"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _select_companies(
    db: Session,
    limit: int,
    company_id: str | None,
    tier: str | None,
) -> list:
    """Return rows with (company_id, canonical_name, state, uei, lead_candidate_id).

    Single-company mode: returns at most one row for the given company_id.
    Batch mode: orders by tier priority, score desc, never-checked first.
    """
    if company_id is not None:
        row = db.execute(
            text("""
                SELECT
                    c.id           AS company_id,
                    c.canonical_name,
                    c.state,
                    ci.id_value    AS uei,
                    lc.id          AS lead_candidate_id
                FROM companies c
                JOIN lead_candidates lc
                  ON lc.company_id = c.id
                 AND lc.status = 'active'
                 AND lc.deleted_at IS NULL
                LEFT JOIN company_identifiers ci
                  ON ci.company_id = c.id
                 AND ci.id_type = 'uei'
                WHERE c.id = :company_id
                  AND c.deleted_at IS NULL
                LIMIT 1
            """),
            {"company_id": str(company_id)},
        ).fetchone()
        return [row] if row is not None else []

    _BASE_SELECT = """
            SELECT
                c.id           AS company_id,
                c.canonical_name,
                c.state,
                ci.id_value    AS uei,
                lc.id          AS lead_candidate_id
            FROM lead_candidates lc
            JOIN companies c ON c.id = lc.company_id
            LEFT JOIN company_identifiers ci
              ON ci.company_id = c.id
             AND ci.id_type = 'uei'
            LEFT JOIN company_contactability cc ON cc.company_id = c.id
            WHERE lc.status = 'active'
              AND lc.deleted_at IS NULL
              AND c.deleted_at IS NULL
    """
    _ORDER_LIMIT = """
            ORDER BY
                CASE lc.tier WHEN 'hot' THEN 1 WHEN 'warm' THEN 2 WHEN 'cold' THEN 3 ELSE 4 END ASC,
                lc.current_score DESC NULLS LAST,
                cc.last_checked_at ASC NULLS FIRST
            LIMIT :limit
    """

    if tier:
        sql = text(_BASE_SELECT + "              AND lc.tier = :tier\n" + _ORDER_LIMIT)
        params: dict = {"limit": limit, "tier": tier}
    else:
        sql = text(_BASE_SELECT + _ORDER_LIMIT)
        params = {"limit": limit}

    rows = db.execute(sql, params).fetchall()
    return list(rows)


def _enrich_one(
    row,
    run_id: UUID,
    source: str,
    search_provider,
    sam_provider,
    db: Session,
) -> str:
    """Enrich one company and upsert the company_contactability row.

    Returns an outcome string:
      "enriched"      — a useful result was stored.
      "rate_limited"  — SAM was throttled and nothing else was learned; no write.
      "error"         — SAM lookup failed and nothing else was learned; no write.

    Provider failures (throttle / API error) with no other useful signal do NOT
    write a row: a throttle is not proof the company is uncontactable, and we
    must not overwrite an existing good row with a misleading one (option A).
    """
    company_id = row.company_id
    company_name = row.canonical_name
    state = row.state
    uei = row.uei
    lead_candidate_id = row.lead_candidate_id

    log = logger.bind(company_id=str(company_id), company_name=company_name)

    # SAM lookup
    sam_result: SAMResult | None = None
    if source in ("sam", "all"):
        sam_result = sam_provider.lookup_entity(uei, dry_run=False)
        log.info("contactability_sam_result", match_status=sam_result.match_status)

    # Website search
    website_result: WebsiteResult | None = None
    if source in ("search", "all"):
        website_result = search_provider.find_company_website(company_name, state, dry_run=False)
        if website_result:
            log.info("contactability_website_found", url=website_result.url)

    # Website contact extraction (only if website was found)
    contact_result: WebsiteContactResult = WebsiteContactResult()
    if website_result:
        contact_result = extract_website_contacts(website_result.url, dry_run=False)

    official_website = website_result.url if website_result else None

    # Honest failure handling: if SAM was the source of truth for this run and it
    # failed (throttle / API error) AND we learned nothing else, do not write a
    # row. Storing not_contactable here would be a lie, and overwriting an
    # existing good row would destroy real data.
    sam_failed = sam_result is not None and sam_result.match_status in _SAM_PROVIDER_FAILURE
    learned_something = bool(official_website) or (
        sam_result is not None and sam_result.match_status in _SAM_USEFUL
    )
    if sam_failed and not learned_something:
        log.warning(
            "contactability_provider_failed",
            sam_match_status=sam_result.match_status,
        )
        return sam_result.match_status  # "rate_limited" or "error"

    status = compute_contactability_status(
        official_website=official_website,
        contact_page_url=contact_result.contact_page_url,
        phone=contact_result.phone,
        generic_email=contact_result.generic_email,
        sam_match_status=sam_result.match_status if sam_result else None,
    )
    score = compute_contactability_score(
        official_website=official_website,
        contact_page_url=contact_result.contact_page_url,
        phone=contact_result.phone,
        generic_email=contact_result.generic_email,
        sam_match_status=sam_result.match_status if sam_result else None,
    )

    # Notes double as a lightweight evidence trail. SAMResult has no source_url
    # field, so the SAM.gov source URL (api_key omitted) is recorded here on a
    # confirmed match — this is the one schema field that can hold it.
    note_parts: list[str] = []
    if sam_result and sam_result.match_status == "matched" and sam_result.uei:
        note_parts.append(f"sam_source={SAMGovProvider.BASE_URL}?ueiSAM={sam_result.uei}")
    if contact_result.robots_blocked:
        note_parts.append("robots_blocked=True")
    notes = "; ".join(note_parts) or None
    now = _utcnow()

    existing: CompanyContactability | None = db.execute(
        select(CompanyContactability).where(
            CompanyContactability.company_id == company_id
        )
    ).scalar_one_or_none()

    if existing is None:
        record = CompanyContactability(
            id=_uuid_module.uuid4(),
            company_id=company_id,
            lead_candidate_id=lead_candidate_id,
            official_website=official_website,
            website_confidence=website_result.confidence if website_result else None,
            website_source=website_result.source if website_result else None,
            website_source_url=website_result.source_url if website_result else None,
            contact_page_url=contact_result.contact_page_url,
            phone=contact_result.phone,
            phone_source_url=contact_result.phone_source_url,
            generic_email=contact_result.generic_email,
            email_source_url=contact_result.email_source_url,
            address_from_website=contact_result.address,
            sam_uei=sam_result.uei if sam_result else uei,
            sam_match_status=sam_result.match_status if sam_result else None,
            sam_registration_status=sam_result.registration_status if sam_result else None,
            sam_address=sam_result.address if sam_result else None,
            contactability_status=status,
            contactability_score=score,
            contactability_notes=notes,
            enrichment_run_id=run_id,
            last_checked_at=now,
        )
        db.add(record)
    else:
        existing.lead_candidate_id = lead_candidate_id
        existing.official_website = official_website
        existing.website_confidence = website_result.confidence if website_result else None
        existing.website_source = website_result.source if website_result else None
        existing.website_source_url = website_result.source_url if website_result else None
        existing.contact_page_url = contact_result.contact_page_url
        existing.phone = contact_result.phone
        existing.phone_source_url = contact_result.phone_source_url
        existing.generic_email = contact_result.generic_email
        existing.email_source_url = contact_result.email_source_url
        existing.address_from_website = contact_result.address
        existing.sam_uei = sam_result.uei if sam_result else uei
        existing.sam_match_status = sam_result.match_status if sam_result else None
        existing.sam_registration_status = sam_result.registration_status if sam_result else None
        existing.sam_address = sam_result.address if sam_result else None
        existing.contactability_status = status
        existing.contactability_score = score
        existing.contactability_notes = notes
        existing.enrichment_run_id = run_id
        existing.last_checked_at = now

    db.flush()
    log.info("contactability_enriched", status=status, score=score)
    return "enriched"


def run_contactability_enrichment(
    db: Session,
    limit: int,
    company_id: str | None = None,
    tier: str | None = "warm",
    source: str = "all",
    dry_run: bool = True,
    apply: bool = False,
    search_provider=None,
    sam_provider=None,
) -> dict:
    """Enrich active lead candidates with contactability data.

    dry_run=True: count candidates only; no writes, no HTTP calls.
    apply=False:  forces dry_run=True regardless of the dry_run parameter.

    Returns a summary dict with "dry_run" key always present.
    """
    if not apply:
        dry_run = True

    if search_provider is None:
        search_provider = BraveSearchProvider()
    if sam_provider is None:
        sam_provider = SAMGovProvider()

    companies = _select_companies(db, limit, company_id, tier)

    if dry_run:
        logger.info(
            "contactability_dry_run",
            companies_would_enrich=len(companies),
            tier=tier,
            source=source,
            limit=limit,
        )
        return {
            "dry_run": True,
            "companies_would_enrich": len(companies),
            "tier": tier,
            "source": source,
            "limit": limit,
        }

    # Apply mode — create an audit run record
    run = ContactabilityRun(
        id=_uuid_module.uuid4(),
        status="running",
        trigger="cli",
        source_filter=source,
        tier_filter=tier,
        company_limit=limit,
        dry_run=False,
        companies_attempted=len(companies),
    )
    db.add(run)
    db.flush()

    enriched = 0
    failed = 0
    failure_reasons: dict[str, int] = {}

    for row in companies:
        try:
            outcome = _enrich_one(row, run.id, source, search_provider, sam_provider, db)
        except Exception as exc:
            outcome = "exception"
            logger.error(
                "contactability_enrich_failed",
                company_id=str(row.company_id),
                error=str(exc),
            )
            capture_exception(exc, {"company_id": str(row.company_id)})

        if outcome == "enriched":
            enriched += 1
        else:
            failed += 1
            failure_reasons[outcome] = failure_reasons.get(outcome, 0) + 1

    run.finished_at = _utcnow()
    run.status = "completed"
    run.companies_enriched = enriched
    run.companies_failed = failed
    if failure_reasons:
        run.error_summary = "; ".join(
            f"{reason}={count}" for reason, count in sorted(failure_reasons.items())
        )
    db.commit()

    logger.info(
        "contactability_run_complete",
        run_id=str(run.id),
        enriched=enriched,
        failed=failed,
        failure_reasons=failure_reasons,
    )

    return {
        "dry_run": False,
        "run_id": str(run.id),
        "companies_attempted": len(companies),
        "companies_enriched": enriched,
        "companies_failed": failed,
        "companies_failed_breakdown": failure_reasons,
        "tier": tier,
        "source": source,
    }
