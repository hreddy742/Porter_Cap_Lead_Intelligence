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
_FRESHNESS_WINDOW_DAYS = 180


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


def _compute_freshness(action_date: date) -> float:
    """Clamp to [0.0, 1.0]; future dates (negative days_old) are treated as maximally fresh."""
    days_old = (date.today() - action_date).days
    return min(1.0, max(0.0, round(1.0 - (days_old / _FRESHNESS_WINDOW_DAYS), 4)))


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


def extract_evidence(raw_event_id: UUID, db: Session) -> list[EvidenceItem]:
    """
    Extract structured evidence from one raw_source_events row.

    Dispatches to the correct handler based on payload shape:
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

    if "recipient_name" in payload and "Recipient Name" not in payload:
        return _extract_subaward_evidence(raw_event, payload, log, db)
    return _extract_prime_award_evidence(raw_event, payload, log, db)
