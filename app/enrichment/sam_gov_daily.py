"""
SAM.gov daily enrichment job — subaward company NAICS enrichment.

Runs once per day. Selects up to 20 companies that have SUBCONTRACT_AWARD
signals but no NAICS code, enriches them via SAM.gov entity name search,
stores results in company_contactability, and rescores any company whose
NAICS was updated (bypassing gate 9 since an active lead_candidate already
exists for each of these companies).

API key rotation:
  companies 0-9   → SAM_GOV_API_KEY_HARSHA
  companies 10-19 → SAM_GOV_API_KEY_KATE

Both keys are capped at 10 requests/day per SAM.gov key policy.
"""
from __future__ import annotations

import os
import time
import uuid as _uuid_module
from datetime import date, datetime, timezone
from urllib.parse import urlparse
from uuid import UUID

import httpx
import structlog
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.models import (
    Company,
    CompanyContactability,
    CompanyIdentifier,
    EvidenceItem,
    LeadCandidate,
    LeadScore,
    ScoringConfig,
    Signal,
)
from app.db.session import SessionLocal
from app.processing.scoring import (
    _AR_FIT_PHASE1_CAP,
    _AWARD_SIGNAL_TYPES,
    _assign_tier,
    _check_integrity,
    _dedup_uuids,
    _is_ar_heavy_naics,
    _uuids_to_strings,
)

log = structlog.get_logger(__name__)

SAM_BASE_URL = "https://api.sam.gov/entity-information/v3/entities"
_REQUESTS_PER_KEY = 10
_INTER_REQUEST_DELAY = 1.0  # seconds between API calls
_BACKOFF_BASE = 2.0         # 429 backoff: 2^attempt seconds


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _extract_domain(url: str | None) -> str | None:
    """Extract hostname from a URL. Returns None if URL is blank or unparseable."""
    if not url:
        return None
    try:
        raw = url if url.startswith(("http://", "https://")) else f"https://{url}"
        return urlparse(raw).netloc or None
    except Exception:
        return None


def _select_candidates(db: Session) -> list:
    """
    Return up to 20 subaward companies with no NAICS code, not enriched today.

    Ordered by current_score DESC so we enrich the best leads first.
    """
    today = date.today().isoformat()
    rows = db.execute(
        text("""
            SELECT DISTINCT
                c.id              AS company_id,
                c.canonical_name,
                c.state           AS company_state,
                c.naics_code,
                c.website_domain,
                lc.id             AS lead_candidate_id,
                lc.current_score
            FROM companies c
            JOIN signals s
              ON s.company_id = c.id
             AND s.signal_type = 'SUBCONTRACT_AWARD'
            JOIN lead_candidates lc
              ON lc.company_id = c.id
             AND lc.status = 'active'
             AND lc.deleted_at IS NULL
            LEFT JOIN company_contactability cc
              ON cc.company_id = c.id
            WHERE c.naics_code IS NULL
              AND c.deleted_at IS NULL
              AND (
                    cc.id IS NULL
                    OR DATE(cc.last_checked_at AT TIME ZONE 'UTC') < :today
                  )
            ORDER BY lc.current_score DESC NULLS LAST
            LIMIT 20
        """),
        {"today": today},
    ).fetchall()
    return list(rows)


def _lookup_by_name(company_name: str, api_key: str) -> tuple[dict | None, str]:
    """
    Call SAM.gov entity search by legalBusinessName for active registrations.

    Returns (entity_dict | None, status).
    Status values: 'matched' | 'not_found' | 'rate_limited' | 'error'
    """
    params = {
        "api_key": api_key,
        "legalBusinessName": company_name,
        "includeSections": "entityRegistration,coreData,assertions,pointsOfContact",
        "registrationStatus": "A",
    }
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(SAM_BASE_URL, params=params)
    except Exception as exc:
        log.error("sam_daily_request_error", company_name=company_name, error=str(exc))
        return None, "error"

    if resp.status_code == 429:
        return None, "rate_limited"
    if resp.status_code >= 400:
        log.error(
            "sam_daily_http_error",
            company_name=company_name,
            status_code=resp.status_code,
        )
        return None, "error"

    try:
        data = resp.json()
    except Exception:
        log.error("sam_daily_parse_error", company_name=company_name)
        return None, "error"

    entities = data.get("entityData") or []
    if not entities:
        return None, "not_found"

    return entities[0], "matched"


def _extract_fields(entity: dict) -> dict:
    """Parse a SAM.gov entityData entry into a flat fields dict."""
    registration = entity.get("entityRegistration") or {}
    core = entity.get("coreData") or {}
    assertions = entity.get("assertions") or {}
    poc = entity.get("pointsOfContact") or {}

    naics = (assertions.get("goodsAndServices") or {}).get("primaryNaics")
    uei = registration.get("ueiSAM")
    registration_status = registration.get("registrationStatus")
    exclusion_flag = registration.get("exclusionStatusFlag")

    website_url = (core.get("entityInformation") or {}).get("entityURL")
    website_domain = _extract_domain(website_url)

    physical = core.get("physicalAddress") or {}
    state = physical.get("stateOrProvinceCode")

    gov_poc = poc.get("governmentBusinessPOC") or {}
    first = (gov_poc.get("firstName") or "").strip()
    last = (gov_poc.get("lastName") or "").strip()
    title = (gov_poc.get("title") or "").strip()
    ceo_name = " ".join(filter(None, [first, last])) or None
    ceo_note = f"{ceo_name} ({title})" if ceo_name and title else ceo_name

    return {
        "naics": str(naics) if naics else None,
        "uei": uei,
        "registration_status": registration_status,
        "exclusion_flag": exclusion_flag,
        "website_url": website_url,
        "website_domain": website_domain,
        "state": state,
        "ceo_note": ceo_note,
    }


def _upsert_contactability(
    db: Session,
    company_id: UUID,
    lead_candidate_id: UUID,
    sam_match_status: str,
    fields: dict | None,
) -> None:
    """Insert or update the company_contactability row for a SAM enrichment result."""
    now = _utcnow()

    if sam_match_status == "matched" and fields:
        contactability_status = "partially_contactable"
        contactability_score = 3
        official_website = fields.get("website_url")
        notes = fields.get("ceo_note")
        sam_uei = fields.get("uei")
        sam_reg_status = fields.get("registration_status")
    else:
        contactability_status = "not_contactable"
        contactability_score = 0
        official_website = None
        notes = None
        sam_uei = None
        sam_reg_status = None

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
            sam_uei=sam_uei,
            sam_match_status=sam_match_status,
            sam_registration_status=sam_reg_status,
            contactability_status=contactability_status,
            contactability_score=contactability_score,
            contactability_notes=notes,
            last_checked_at=now,
        )
        db.add(record)
    else:
        if official_website:
            existing.official_website = official_website
        if sam_uei:
            existing.sam_uei = sam_uei
        existing.sam_match_status = sam_match_status
        existing.sam_registration_status = sam_reg_status
        existing.contactability_status = contactability_status
        existing.contactability_score = contactability_score
        existing.contactability_notes = notes
        existing.last_checked_at = now


def _update_company_fields(db: Session, company: Company, fields: dict) -> bool:
    """
    Update Company columns from SAM data where currently None.

    Returns True if naics_code was updated (caller should trigger rescore).
    Inserts a CompanyIdentifier(id_type='uei') if no UEI is on file and the
    value is not already claimed by another company.
    """
    naics_updated = False

    if company.naics_code is None and fields.get("naics"):
        company.naics_code = fields["naics"]
        naics_updated = True

    if company.state is None and fields.get("state"):
        company.state = fields["state"]

    if company.website_domain is None and fields.get("website_domain"):
        company.website_domain = fields["website_domain"]

    if fields.get("uei"):
        existing_uei = db.execute(
            select(CompanyIdentifier).where(
                CompanyIdentifier.company_id == company.id,
                CompanyIdentifier.id_type == "uei",
            )
        ).scalar_one_or_none()
        if existing_uei is None:
            # Guard against UEI already belonging to a different company.
            conflict = db.execute(
                select(CompanyIdentifier).where(
                    CompanyIdentifier.id_type == "uei",
                    CompanyIdentifier.id_value == fields["uei"],
                )
            ).scalar_one_or_none()
            if conflict is None:
                db.add(
                    CompanyIdentifier(
                        id=_uuid_module.uuid4(),
                        company_id=company.id,
                        id_type="uei",
                        id_value=fields["uei"],
                    )
                )

    return naics_updated


def _rescore_after_naics(company_id: UUID, db: Session) -> dict:
    """
    Insert a new versioned LeadScore and update LeadCandidate after NAICS enrichment.

    Cannot use score_company() here: gate 9 (duplicate_active) correctly blocks
    re-scoring of companies that already have an active LeadCandidate. This
    function mirrors the same six-component scoring math but skips the gates,
    consistent with the rescore_stale_leads.py pattern.

    Returns a dict with 'rescored', 'total_score', 'tier', 'old_tier'.
    """
    config_row = db.execute(
        select(ScoringConfig).where(ScoringConfig.active == True)  # noqa: E712
    ).scalars().first()
    if config_row is None:
        log.error("sam_rescore_no_active_config", company_id=str(company_id))
        return {"rescored": False, "reason": "no_active_config"}

    company: Company | None = db.get(Company, company_id)
    if company is None:
        return {"rescored": False, "reason": "company_not_found"}

    candidate: LeadCandidate | None = db.execute(
        select(LeadCandidate).where(
            LeadCandidate.company_id == company_id,
            LeadCandidate.status == "active",
            LeadCandidate.deleted_at.is_(None),
        )
    ).scalars().first()
    if candidate is None:
        return {"rescored": False, "reason": "no_active_candidate"}

    evidence_items: list[EvidenceItem] = (
        db.execute(select(EvidenceItem).where(EvidenceItem.company_id == company_id))
        .scalars()
        .all()
    )
    signals: list[Signal] = (
        db.execute(select(Signal).where(Signal.company_id == company_id))
        .scalars()
        .all()
    )

    all_evidence_ids: list[UUID] = [e.id for e in evidence_items]
    contract_signals = [s for s in signals if s.signal_type in _AWARD_SIGNAL_TYPES]
    award_signals = [s for s in signals if s.award_amount is not None]
    cited_uuids: set[UUID] = set()
    components: dict = {}

    # ── Component 1: Porter Fit (max 25) ──────────────────────────────────────
    pf_points = 0
    pf_evidence: list[UUID] = []
    if _is_ar_heavy_naics(company.naics_code):
        pf_points += 15
        pf_evidence.extend(all_evidence_ids)
    if award_signals:
        best = max(award_signals, key=lambda s: float(s.award_amount or 0))
        amount = float(best.award_amount)
        if 250_000 <= amount <= 10_000_000:
            pf_points += 5
            pf_evidence.append(best.evidence_id)
    if not company.country or company.country == "US":
        pf_points += 5
        pf_evidence.extend(all_evidence_ids)
    _check_integrity("porter_fit", pf_points, pf_evidence)
    cited_uuids.update(pf_evidence)
    components["porter_fit"] = {
        "points": pf_points,
        "max": 25,
        "evidence_ids": _uuids_to_strings(pf_evidence),
    }

    # ── Component 2: Why-Now / Timing (max 30) ────────────────────────────────
    wn_points = 0
    wn_evidence: list[UUID] = []
    if contract_signals:
        freshest = max(contract_signals, key=lambda s: float(s.freshness_score))
        fs = float(freshest.freshness_score)
        if fs >= 0.8:
            wn_points = 30
        elif fs >= 0.5:
            wn_points = 18
        elif fs >= 0.1:
            wn_points = 8
        if wn_points > 0:
            wn_evidence.append(freshest.evidence_id)
    _check_integrity("why_now", wn_points, wn_evidence)
    cited_uuids.update(wn_evidence)
    components["why_now"] = {
        "points": wn_points,
        "max": 30,
        "evidence_ids": _uuids_to_strings(wn_evidence),
    }

    # ── Component 3: A/R Financing Fit (max 15, Phase 1 cap = 10) ────────────
    ar_points = 0
    ar_evidence: list[UUID] = []
    if _is_ar_heavy_naics(company.naics_code):
        ar_points += 7
        ar_evidence.extend(all_evidence_ids)
    if contract_signals:
        ar_points += 3
        ar_evidence.append(contract_signals[0].evidence_id)
    ar_points = min(ar_points, _AR_FIT_PHASE1_CAP)
    _check_integrity("ar_fit", ar_points, ar_evidence)
    cited_uuids.update(ar_evidence)
    components["ar_fit"] = {
        "points": ar_points,
        "max": 15,
        "phase1_cap": _AR_FIT_PHASE1_CAP,
        "ar_fit_confidence": "low",
        "evidence_ids": _uuids_to_strings(ar_evidence),
    }

    # ── Components 4 & 5: zero in Phase 1 ────────────────────────────────────
    components["contactability"] = {
        "points": 0,
        "max": 10,
        "evidence_ids": [],
        "note": "Phase 1: contact enrichment not built",
    }
    components["risk_clean"] = {
        "points": 0,
        "max": 10,
        "evidence_ids": [],
        "note": "Phase 1: dedicated risk evidence not available",
    }

    # ── Component 6: Evidence Quality (max 10) ────────────────────────────────
    eq_points = 0
    eq_evidence: list[UUID] = []
    if len(evidence_items) >= 2:
        eq_points += 5
        eq_evidence.extend(all_evidence_ids[:2])
    source_url_items = [e for e in evidence_items if e.source_url]
    if source_url_items:
        eq_points += 3
        eq_evidence.append(source_url_items[0].id)
    _check_integrity("evidence_quality", eq_points, eq_evidence)
    cited_uuids.update(eq_evidence)
    components["evidence_quality"] = {
        "points": eq_points,
        "max": 10,
        "evidence_ids": _uuids_to_strings(eq_evidence),
    }

    total_score = sum(c["points"] for c in components.values())
    tier = _assign_tier(total_score)
    all_cited_uuids = _dedup_uuids(list(cited_uuids))

    new_score = LeadScore(
        id=_uuid_module.uuid4(),
        lead_candidate_id=candidate.id,
        scoring_config_id=config_row.id,
        config_hash=config_row.config_hash,
        total_score=total_score,
        tier=tier,
        component_breakdown=components,
        evidence_ids=all_cited_uuids,
        gate_result="passed",
        gate_reasons=[],
    )
    db.add(new_score)

    old_tier = candidate.tier
    candidate.tier = tier
    candidate.current_score = total_score

    return {
        "rescored": True,
        "total_score": total_score,
        "tier": tier,
        "old_tier": old_tier,
    }


def run_enrichment() -> dict:
    """
    Daily SAM.gov enrichment entry point.

    Enriches up to 20 subaward companies (10 per key), upserts
    company_contactability rows, and rescores any company whose NAICS was updated.

    Returns a summary dict.
    """
    key1 = os.getenv("SAM_GOV_API_KEY_HARSHA")
    key2 = os.getenv("SAM_GOV_API_KEY_KATE")

    if not key1 and not key2:
        log.error("sam_daily_no_api_keys")
        return {"error": "No SAM.gov API keys configured."}

    db: Session = SessionLocal()

    enriched_count = 0
    matched_count = 0
    not_found_count = 0
    rescored_count = 0
    matched_samples: list[dict] = []
    tier_upgrades: list[dict] = []

    try:
        candidates = _select_candidates(db)
        log.info("sam_daily_candidates_found", count=len(candidates))

        if not candidates:
            log.info("sam_daily_no_candidates")
            return {
                "enriched": 0,
                "matched": 0,
                "not_found": 0,
                "rescored": 0,
                "matched_samples": [],
                "tier_upgrades": [],
            }

        for idx, row in enumerate(candidates):
            company_id = row.company_id
            company_name = row.canonical_name

            # Key rotation: first 10 → KEY_1 (Harsha), next 10 → KEY_2 (Kate)
            if idx < _REQUESTS_PER_KEY:
                api_key = key1
                key_label = "harsha"
                fallback_key = key2
            else:
                api_key = key2
                key_label = "kate"
                fallback_key = key1

            # Fall back if the primary key for this slot is missing
            if not api_key:
                api_key = fallback_key
                key_label = "kate" if key_label == "harsha" else "harsha"

            if not api_key:
                log.error("sam_daily_no_key_available", idx=idx)
                break

            log.info(
                "sam_daily_enriching",
                company_name=company_name,
                idx=idx,
                key=key_label,
            )

            # Single attempt; on 429, backoff and try the other key once.
            entity, status = _lookup_by_name(company_name, api_key)

            if status == "rate_limited":
                wait = _BACKOFF_BASE ** 1
                log.warning(
                    "sam_daily_rate_limited",
                    company_name=company_name,
                    wait=wait,
                    key=key_label,
                )
                time.sleep(wait)

                other_key = key2 if key_label == "harsha" else key1
                if other_key:
                    other_label = "kate" if key_label == "harsha" else "harsha"
                    entity, status = _lookup_by_name(company_name, other_key)
                    if status == "rate_limited":
                        wait2 = _BACKOFF_BASE ** 2
                        log.warning(
                            "sam_daily_rate_limited",
                            company_name=company_name,
                            wait=wait2,
                            key=other_label,
                        )
                        time.sleep(wait2)
                        log.error(
                            "sam_daily_both_keys_exhausted",
                            company_name=company_name,
                        )
                        break
                else:
                    log.error(
                        "sam_daily_both_keys_exhausted",
                        company_name=company_name,
                    )
                    break

            # Errors (non-429) are skipped — we don't know if the company is
            # uncontactable just because the API call failed.
            if status == "error":
                log.warning("sam_daily_api_error_skipping", company_name=company_name)
                time.sleep(_INTER_REQUEST_DELAY)
                continue

            enriched_count += 1
            company: Company = db.get(Company, company_id)

            if status == "matched" and entity is not None:
                fields = _extract_fields(entity)

                if fields.get("exclusion_flag") == "Y":
                    log.warning(
                        "sam_daily_excluded_entity",
                        company_name=company_name,
                        uei=fields.get("uei"),
                    )

                naics_updated = _update_company_fields(db, company, fields)
                _upsert_contactability(
                    db, company_id, row.lead_candidate_id, "matched", fields
                )

                log.info(
                    "sam_enrichment_matched",
                    company_name=company_name,
                    naics=fields.get("naics"),
                    uei=fields.get("uei"),
                    ceo_name=fields.get("ceo_note"),
                )
                matched_count += 1

                if len(matched_samples) < 5:
                    matched_samples.append(
                        {
                            "name": company_name,
                            "naics": fields.get("naics"),
                            "ceo": fields.get("ceo_note"),
                        }
                    )

                db.flush()

                if naics_updated:
                    rescore = _rescore_after_naics(company_id, db)
                    if rescore.get("rescored"):
                        rescored_count += 1
                        old_tier = rescore.get("old_tier")
                        new_tier = rescore.get("tier")
                        log.info(
                            "sam_rescore_complete",
                            company_name=company_name,
                            old_tier=old_tier,
                            new_tier=new_tier,
                            score=rescore.get("total_score"),
                        )
                        if old_tier != new_tier:
                            tier_upgrades.append(
                                {
                                    "name": company_name,
                                    "old_tier": old_tier,
                                    "new_tier": new_tier,
                                    "score": rescore.get("total_score"),
                                }
                            )
            else:
                # not_found
                _upsert_contactability(
                    db, company_id, row.lead_candidate_id, "not_found", None
                )
                log.info("sam_enrichment_not_found", company_name=company_name)
                not_found_count += 1
                db.flush()

            db.commit()
            time.sleep(_INTER_REQUEST_DELAY)

    except Exception as exc:
        db.rollback()
        log.error("sam_daily_unexpected_error", error=str(exc))
        raise
    finally:
        db.close()

    summary = {
        "enriched": enriched_count,
        "matched": matched_count,
        "not_found": not_found_count,
        "rescored": rescored_count,
        "matched_samples": matched_samples,
        "tier_upgrades": tier_upgrades,
    }

    log.info(
        "sam_daily_complete",
        enriched=enriched_count,
        matched=matched_count,
        not_found=not_found_count,
        rescored=rescored_count,
        tier_upgrades=len(tier_upgrades),
    )

    return summary
