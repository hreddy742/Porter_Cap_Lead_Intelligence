"""
Evidence extractor — Task 02.

Reads a raw_source_events row and writes one evidence_items row.

Supported payload shapes:
  - USASpending prime awards (spending_by_transaction):
      uses title-case field names ("Recipient Name", "Action Date")
      → claim_supported = "CONTRACT_AWARD"

  - USASpending subawards (/api/v2/subawards/):
      uses lowercase field names ("recipient_name", "action_date")
      → claim_supported = "SUBCONTRACT_AWARD"

Dispatch is by payload key shape — no DB join to source_registry needed.

Quarantine conditions (return [] without raising):
  - action_date missing or unparseable
  - company_name missing or blank

company_id is left NULL here. Task 03 (company resolution) populates it.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from uuid import UUID

import structlog
from sqlalchemy.orm import Session

from app.db.models import EvidenceItem, RawSourceEvent

logger = structlog.get_logger(__name__)

_AWARD_URL_TEMPLATE = "https://www.usaspending.gov/award/{}/"
_CLAIM_CONTRACT_AWARD = "CONTRACT_AWARD"
_CLAIM_SUBCONTRACT_AWARD = "SUBCONTRACT_AWARD"
_CONFIDENCE_API = Decimal("0.9")
_CONFIDENCE_SUBAWARD = Decimal("0.85")  # slightly lower: no UEI, no NAICS in response
_CONFIDENCE_SBA = Decimal("0.85")       # FOIA bulk data, no UEI in response
_CONFIDENCE_SBIR = Decimal("0.90")      # federal grant database — UEI present when awarded
_FRESHNESS_WINDOW_DAYS = 180
# SBA loans span up to 4+ years of history (filter: 2022-01-01+).
# Use a longer freshness window so older loans still exceed the 0.1 Gate 3 floor.
_SBA_FRESHNESS_WINDOW_DAYS = 1825  # 5 years
_SBIR_FRESHNESS_WINDOW_DAYS = 1825  # 5 years — same as SBA; SBIR filter is 2022+
_SBA_SOURCE_URL = "https://data.sba.gov/en/dataset/0ff8e8e9-b967-4f4e-987c-6ac78c575087"
_SBIR_SOURCE_URL = "https://www.sbir.gov/awards"


def _parse_date(value: object) -> date | None:
    """Parse ISO date string or date object. Returns None on any failure."""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except (ValueError, AttributeError):
            return None
    return None


def _compute_freshness(action_date: date, window_days: int = _FRESHNESS_WINDOW_DAYS) -> float:
    """Clamp to [0.0, 1.0]; future dates (negative days_old) are treated as maximally fresh."""
    days_old = (date.today() - action_date).days
    return min(1.0, max(0.0, round(1.0 - (days_old / window_days), 4)))


def _build_evidence_item(
    *,
    raw_event: RawSourceEvent,
    source_url: str,
    extracted_fields: dict,
    claim_supported: str,
    confidence_score: Decimal,
    freshness: float,
) -> EvidenceItem:
    content_hash = hashlib.sha256(
        json.dumps(extracted_fields, sort_keys=True, default=str).encode()
    ).hexdigest()
    return EvidenceItem(
        raw_event_id=raw_event.id,
        source_id=raw_event.source_id,
        company_id=None,
        source_url=source_url,
        extracted_fields=extracted_fields,
        content_hash=content_hash,
        claim_supported=claim_supported,
        confidence_score=confidence_score,
        freshness_score=Decimal(str(freshness)),
    )


def _extract_prime_award_evidence(
    raw_event: RawSourceEvent, payload: dict, log: structlog.BoundLogger, db: Session
) -> list[EvidenceItem]:
    """Handle spending_by_transaction payloads (title-case field names)."""
    company_name_raw = payload.get("Recipient Name")
    if not company_name_raw or not str(company_name_raw).strip():
        log.warning("evidence_quarantine_missing_company_name")
        return []
    company_name = str(company_name_raw).strip()

    # Prefer "Action Date" (real obligation date) over "Start Date" (PoP start,
    # which can be years in the future and produces misleading freshness scores).
    action_date = _parse_date(payload.get("Action Date") or payload.get("Start Date"))
    if action_date is None:
        log.warning(
            "evidence_quarantine_bad_action_date",
            value=payload.get("Action Date") or payload.get("Start Date"),
        )
        return []

    # generated_internal_id is the slug USASpending uses in award detail URLs.
    # Award ID is the PIID and cannot be used as a URL path parameter.
    generated_id = str(payload.get("generated_internal_id", "")).strip()
    award_id = str(payload.get("Award ID", "")).strip()
    url_key = generated_id or award_id
    source_url = _AWARD_URL_TEMPLATE.format(url_key) if url_key else (raw_event.source_url or "")

    award_amount_raw = (
        payload.get("Transaction Amount")
        if "Transaction Amount" in payload
        else payload.get("Award Amount")
    )
    extracted_fields: dict = {
        "company_name": company_name,
        "uei": payload.get("Recipient UEI"),
        "award_amount": str(award_amount_raw) if award_amount_raw is not None else None,
        "naics_code": payload.get("NAICS Code") or payload.get("naics_code"),
        "naics_description": payload.get("NAICS Description") or payload.get("naics_description"),
        "action_date": action_date.isoformat(),
        "state_code": payload.get("pop_state_code") or payload.get("Place of Performance State Code"),
        "award_type": payload.get("Award Type"),
        "awarding_agency": payload.get("Awarding Agency"),
        "action_type": payload.get("Action Type"),
        "action_type_description": payload.get("Action Type Description"),
    }

    freshness = _compute_freshness(action_date)
    evidence = _build_evidence_item(
        raw_event=raw_event,
        source_url=source_url,
        extracted_fields=extracted_fields,
        claim_supported=_CLAIM_CONTRACT_AWARD,
        confidence_score=_CONFIDENCE_API,
        freshness=freshness,
    )
    db.add(evidence)
    db.flush()
    log.info("evidence_extracted", award_id=award_id, freshness=freshness)
    return [evidence]


def _extract_subaward_evidence(
    raw_event: RawSourceEvent, payload: dict, log: structlog.BoundLogger, db: Session
) -> list[EvidenceItem]:
    """Handle /api/v2/subawards/ payloads (lowercase field names).

    Note: UEI, state, NAICS, and prime contractor are NOT available from this
    endpoint. Company resolution will log a warning and use name-only matching.
    """
    company_name_raw = payload.get("recipient_name")
    if not company_name_raw or not str(company_name_raw).strip():
        log.warning("evidence_quarantine_missing_company_name")
        return []
    company_name = str(company_name_raw).strip()

    action_date = _parse_date(payload.get("action_date"))
    if action_date is None:
        log.warning("evidence_quarantine_bad_action_date", value=payload.get("action_date"))
        return []

    record_id = payload.get("id")
    source_url = raw_event.source_url or (
        f"https://www.usaspending.gov/subaward/?id={record_id}" if record_id else ""
    )

    amount_raw = payload.get("amount")
    extracted_fields: dict = {
        "company_name": company_name,
        "uei": None,
        "award_amount": str(amount_raw) if amount_raw is not None else None,
        "action_date": action_date.isoformat(),
        "description": payload.get("description"),
        "subaward_number": payload.get("subaward_number"),
        "prime_contractor": None,
        "naics_code": payload.get("naics_code"),
        "state_code": None,
    }

    freshness = _compute_freshness(action_date)
    evidence = _build_evidence_item(
        raw_event=raw_event,
        source_url=source_url,
        extracted_fields=extracted_fields,
        claim_supported=_CLAIM_SUBCONTRACT_AWARD,
        confidence_score=_CONFIDENCE_SUBAWARD,
        freshness=freshness,
    )
    db.add(evidence)
    db.flush()
    log.info(
        "evidence_extracted",
        subaward_id=record_id,
        subaward_number=payload.get("subaward_number"),
        freshness=freshness,
    )
    return [evidence]


def _extract_sba_evidence(
    raw_event: RawSourceEvent, payload: dict, log: structlog.BoundLogger, db: Session
) -> list[EvidenceItem]:
    """Handle SBA 7(a) FOIA CSV payloads (BorrName key).

    claim_supported is taken from payload["sba_signal_type"]:
      "SBA_LOAN_PIF"    — paid-in-full loan
      "SBA_LOAN_ACTIVE" — active loan with lien on receivables
    """
    company_name_raw = payload.get("BorrName")
    if not company_name_raw or not str(company_name_raw).strip():
        log.warning("evidence_quarantine_missing_company_name")
        return []
    company_name = str(company_name_raw).strip()

    approval_date = _parse_date(payload.get("ApprovalDate"))
    if approval_date is None:
        log.warning("evidence_quarantine_bad_action_date", value=payload.get("ApprovalDate"))
        return []

    claim_supported = payload.get("sba_signal_type", "SBA_LOAN_ACTIVE")
    amount_raw = payload.get("GrossApproval")
    loan_status = payload.get("LoanStatus") or ""

    extracted_fields: dict = {
        "company_name": company_name,
        "uei": None,
        "award_amount": str(amount_raw) if amount_raw is not None else None,
        "loan_amount": str(amount_raw) if amount_raw is not None else None,
        "action_date": approval_date.isoformat(),
        "approval_date": approval_date.isoformat(),
        "loan_status": loan_status,
        "naics_code": payload.get("NaicsCode"),
        "naics_description": payload.get("NaicsDescription"),
        "state_code": payload.get("BorrState"),
        "city": payload.get("BorrCity"),
        "zip": payload.get("BorrZip"),
        "street": payload.get("BorrStreet"),
        "jobs_supported": payload.get("JobsSupported"),
        "sba_pif": loan_status.upper() == "PIF",
        "description": payload.get("description"),
    }

    # Use a longer freshness window for SBA loans since the data covers 4+ years
    freshness = _compute_freshness(approval_date, window_days=_SBA_FRESHNESS_WINDOW_DAYS)
    source_url = raw_event.source_url or _SBA_SOURCE_URL

    evidence = _build_evidence_item(
        raw_event=raw_event,
        source_url=source_url,
        extracted_fields=extracted_fields,
        claim_supported=claim_supported,
        confidence_score=_CONFIDENCE_SBA,
        freshness=freshness,
    )
    db.add(evidence)
    db.flush()
    log.info(
        "evidence_extracted",
        claim=claim_supported,
        company=company_name,
        state=payload.get("BorrState"),
        freshness=freshness,
    )
    return [evidence]


def _extract_sbir_evidence(
    raw_event: RawSourceEvent, payload: dict, log: structlog.BoundLogger, db: Session
) -> list[EvidenceItem]:
    """Handle SBIR/STTR grant payloads (sbir_signal_type key).

    Uses award_year to derive a mid-year signal date (June 15).  The full
    year precision is all the API provides; mid-year is a reasonable proxy.
    """
    company_name_raw = payload.get("firm")
    if not company_name_raw or not str(company_name_raw).strip():
        log.warning("evidence_quarantine_missing_company_name")
        return []
    company_name = str(company_name_raw).strip()

    award_year_raw = payload.get("award_year")
    if award_year_raw is None:
        log.warning("evidence_quarantine_bad_action_date", value=award_year_raw)
        return []
    try:
        award_year = int(str(award_year_raw).strip())
    except (ValueError, TypeError):
        log.warning("evidence_quarantine_bad_action_date", value=award_year_raw)
        return []

    # Use June 15 of the award year as the signal date (mid-year proxy)
    action_date_str = f"{award_year}-06-15"
    action_date = _parse_date(action_date_str)
    if action_date is None:
        log.warning("evidence_quarantine_bad_action_date", value=action_date_str)
        return []

    amount_raw = payload.get("award_amount")
    extracted_fields: dict = {
        "company_name": company_name,
        "uei": payload.get("uei"),
        "duns": payload.get("duns"),
        "award_amount": str(amount_raw) if amount_raw is not None else None,
        "action_date": action_date.isoformat(),
        "award_year": award_year,
        "agency": payload.get("agency"),
        "branch": payload.get("branch"),
        "phase": payload.get("phase"),
        "program": payload.get("program"),
        "award_title": payload.get("award_title"),
        "abstract": payload.get("abstract"),
        "state_code": payload.get("state"),
        "city": payload.get("city"),
        "zip": payload.get("zip"),
        "sbir_signal_strength": payload.get("sbir_signal_strength", "medium"),
        "poc_name": payload.get("poc_name"),
        "poc_title": payload.get("poc_title"),
        "poc_phone": payload.get("poc_phone"),
        "poc_email": payload.get("poc_email"),
        "company_url": payload.get("company_url"),
        "employee_count": payload.get("number_employees"),
        "description": payload.get("description"),
    }

    freshness = _compute_freshness(action_date, window_days=_SBIR_FRESHNESS_WINDOW_DAYS)
    source_url = raw_event.source_url or _SBIR_SOURCE_URL

    evidence = _build_evidence_item(
        raw_event=raw_event,
        source_url=source_url,
        extracted_fields=extracted_fields,
        claim_supported="SBIR_GRANT",
        confidence_score=_CONFIDENCE_SBIR,
        freshness=freshness,
    )
    db.add(evidence)
    db.flush()
    log.info(
        "evidence_extracted",
        claim="SBIR_GRANT",
        company=company_name,
        award_year=award_year,
        phase=payload.get("phase"),
        freshness=freshness,
    )
    return [evidence]


def extract_evidence(raw_event_id: UUID, db: Session) -> list[EvidenceItem]:
    """
    Extract structured evidence from one raw_source_events row.

    Dispatches to the correct handler based on payload shape:
      - "sbir_signal_type" key → SBIR/STTR grant → SBIR_GRANT
      - "BorrName" key → SBA 7(a) loan → SBA_LOAN_PIF or SBA_LOAN_ACTIVE
      - lowercase "recipient_name" key → subaward → SUBCONTRACT_AWARD
      - title-case "Recipient Name" key → prime award → CONTRACT_AWARD

    Adds the EvidenceItem to the session and flushes.
    Returns [] and logs a warning for any quarantine condition.
    Never raises.
    """
    raw_event: RawSourceEvent | None = db.get(RawSourceEvent, raw_event_id)
    if raw_event is None:
        logger.warning("evidence_raw_event_not_found", raw_event_id=str(raw_event_id))
        return []

    payload: dict = raw_event.payload or {}
    log = logger.bind(raw_event_id=str(raw_event_id))

    if "sbir_signal_type" in payload:
        return _extract_sbir_evidence(raw_event, payload, log, db)
    if "BorrName" in payload:
        return _extract_sba_evidence(raw_event, payload, log, db)
    if "recipient_name" in payload and "Recipient Name" not in payload:
        return _extract_subaward_evidence(raw_event, payload, log, db)
    return _extract_prime_award_evidence(raw_event, payload, log, db)
