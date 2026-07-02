"""
Company resolution — Task 03.

Reads an evidence_items row, resolves or creates the matching Company,
and populates evidence_items.company_id.

Hard-ID priority (auto-merge ONLY on these):
  1. UEI
  2. domain
  3. state_entity_id

Fuzzy name similarity > 0.85 with no hard ID match:
  - Creates a NEW company (never merges)
  - Inserts a duplicate_review row for human review
"""

from __future__ import annotations

import hashlib
from difflib import SequenceMatcher
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Company, CompanyIdentifier, DuplicateReview, EvidenceItem
from app.utils.normalize import normalize_company_name

logger = structlog.get_logger(__name__)

_FUZZY_THRESHOLD = 0.85


def _str_or_none(val: object) -> str | None:
    """Return stripped string or None for empty/None values."""
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def _compute_external_id(
    uei: str | None,
    domain: str | None,
    normalized_name: str,
    state: str | None,
) -> str:
    """
    Compute external_id once at company creation. Never recomputed.
    Priority: UEI → domain → normalized_name+state
    """
    if uei:
        key = f"uei:{uei}"
    elif domain:
        key = f"domain:{domain}"
    else:
        key = f"name:{normalized_name}|state:{(state or '').upper()}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _find_by_hard_id(id_type: str, id_value: str, db: Session) -> Company | None:
    """Look up a company by a hard identifier. Returns None if not found."""
    stmt = select(CompanyIdentifier).where(
        CompanyIdentifier.id_type == id_type,
        CompanyIdentifier.id_value == id_value,
    )
    identifier = db.execute(stmt).scalar_one_or_none()
    if identifier is None:
        return None
    return db.get(Company, identifier.company_id)


def _fuzzy_candidates(
    normalized_name: str, exclude_id: UUID, db: Session, state: str | None = None
) -> list[tuple[Company, float]]:
    """
    Return active companies with name similarity > threshold.
    Excludes `exclude_id` to prevent a company matching itself after flush.

    Narrowed to companies in the same `state` (indexed) when known — without
    this, comparing every new company against the entire companies table is
    O(new_companies x total_companies) and becomes minutes-to-hours slow once
    the table has thousands of rows (e.g. a full SBA bulk-loan run).
    """
    stmt = select(Company).where(Company.deleted_at.is_(None))
    if state:
        stmt = stmt.where(Company.state == state)
    all_companies = db.execute(stmt).scalars().all()
    results = []
    for c in all_companies:
        if c.id == exclude_id:
            continue
        sim = SequenceMatcher(None, normalized_name, c.normalized_name).ratio()
        if sim > _FUZZY_THRESHOLD:
            results.append((c, sim))
    return results


def resolve_company_for_evidence(evidence_id: UUID, db: Session) -> Company | None:
    """
    Resolve or create the Company for an evidence_items row.
    Sets evidence_items.company_id and flushes.
    Returns None if company_name is missing or evidence not found.
    Does not create signals — that is Task 04.
    """
    evidence: EvidenceItem | None = db.get(EvidenceItem, evidence_id)
    if evidence is None:
        logger.warning("resolution_evidence_not_found", evidence_id=str(evidence_id))
        return None

    fields: dict = evidence.extracted_fields or {}
    log = logger.bind(evidence_id=str(evidence_id))

    company_name_raw = _str_or_none(fields.get("company_name"))
    if not company_name_raw:
        log.warning("resolution_missing_company_name")
        return None

    try:
        normalized_name = normalize_company_name(company_name_raw)
    except ValueError:
        log.warning("resolution_normalize_failed", company_name=company_name_raw)
        return None

    uei = _str_or_none(fields.get("uei"))
    domain = _str_or_none(fields.get("domain"))
    state_entity_id = _str_or_none(fields.get("state_entity_id"))
    state_raw = _str_or_none(fields.get("state_code"))
    state = state_raw.upper() if state_raw else None
    naics_code = _str_or_none(fields.get("naics_code"))
    naics_description = _str_or_none(fields.get("naics_description"))

    # Hard-ID matching: UEI → domain → state_entity_id (first match wins)
    company: Company | None = None
    if uei:
        company = _find_by_hard_id("uei", uei, db)
    if company is None and domain:
        company = _find_by_hard_id("domain", domain, db)
    if company is None and state_entity_id:
        company = _find_by_hard_id("state_entity_id", state_entity_id, db)

    if company is not None:
        # Fill null NAICS fields if evidence provides them — never overwrite existing data.
        if not company.naics_code and naics_code:
            company.naics_code = naics_code
        if not company.naics_description and naics_description:
            company.naics_description = naics_description
        evidence.company_id = company.id
        db.flush()
        log.info("resolution_matched_existing", company_id=str(company.id))
        return company

    # No hard match → compute external_id and check if company already exists.
    # This handles re-runs: the same name+state produces the same external_id, and
    # we should return the existing record rather than raise UniqueViolation.
    external_id = _compute_external_id(uei, domain, normalized_name, state)
    existing_by_ext_id = db.execute(
        select(Company).where(Company.external_id == external_id)
    ).scalar_one_or_none()
    if existing_by_ext_id is not None:
        if not existing_by_ext_id.naics_code and naics_code:
            existing_by_ext_id.naics_code = naics_code
        if not existing_by_ext_id.naics_description and naics_description:
            existing_by_ext_id.naics_description = naics_description
        evidence.company_id = existing_by_ext_id.id
        db.flush()
        log.info("resolution_matched_by_external_id", company_id=str(existing_by_ext_id.id))
        return existing_by_ext_id

    company = Company(
        canonical_name=company_name_raw,
        normalized_name=normalized_name,
        external_id=external_id,
        state=state,
        naics_code=naics_code,
        naics_description=naics_description,
    )
    db.add(company)
    db.flush()  # Assigns company.id before creating dependent rows

    if uei:
        db.add(CompanyIdentifier(company_id=company.id, id_type="uei", id_value=uei))
    if domain:
        db.add(CompanyIdentifier(company_id=company.id, id_type="domain", id_value=domain))
    if state_entity_id:
        db.add(
            CompanyIdentifier(
                company_id=company.id,
                id_type="state_entity_id",
                id_value=state_entity_id,
            )
        )
    db.flush()

    # Fuzzy check — flag for human review, NEVER auto-merge
    for match, sim in _fuzzy_candidates(normalized_name, company.id, db, state=state):
        # Canonical pair ordering: smaller UUID string is always company_id_a
        if str(company.id) < str(match.id):
            id_a, id_b = company.id, match.id
            name_a, name_b = normalized_name, match.normalized_name
            state_a, state_b = state, match.state
        else:
            id_a, id_b = match.id, company.id
            name_a, name_b = match.normalized_name, normalized_name
            state_a, state_b = match.state, state

        already_flagged = db.execute(
            select(DuplicateReview).where(
                DuplicateReview.company_id_a == id_a,
                DuplicateReview.company_id_b == id_b,
                DuplicateReview.status == "pending",
            )
        ).scalar_one_or_none()

        if already_flagged is None:
            db.add(
                DuplicateReview(
                    company_id_a=id_a,
                    company_id_b=id_b,
                    similarity_score=round(sim, 3),
                    match_basis="name_similarity",
                    normalized_name_a=name_a,
                    normalized_name_b=name_b,
                    state_a=state_a,
                    state_b=state_b,
                )
            )

    evidence.company_id = company.id
    db.flush()

    log.info(
        "resolution_created_new_company",
        company_id=str(company.id),
        canonical_name=company.canonical_name,
    )
    return company
