"""
Evidence extractor — Task 02.

Reads a raw_source_events row and writes one evidence_items row.
For USASpending records: one raw event → one CONTRACT_AWARD evidence item.

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
_CONFIDENCE_API = Decimal("0.9")
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
    """max(0.0, round(1.0 - (days_old / 180), 4))"""
    days_old = (date.today() - action_date).days
    return max(0.0, round(1.0 - (days_old / _FRESHNESS_WINDOW_DAYS), 4))


def extract_evidence(raw_event_id: UUID, db: Session) -> list[EvidenceItem]:
    """
    Extract structured evidence from one raw_source_events row.

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

    # Quarantine: missing or blank company_name
    company_name_raw = payload.get("Recipient Name")
    if not company_name_raw or not str(company_name_raw).strip():
        log.warning("evidence_quarantine_missing_company_name")
        return []
    company_name = str(company_name_raw).strip()

    # Quarantine: missing or unparseable action_date
    action_date = _parse_date(payload.get("Start Date"))
    if action_date is None:
        log.warning(
            "evidence_quarantine_bad_action_date",
            value=payload.get("Start Date"),
        )
        return []

    # Build source URL from Award ID; fall back to raw_event.source_url
    award_id = str(payload.get("Award ID", "")).strip()
    source_url = _AWARD_URL_TEMPLATE.format(award_id) if award_id else (raw_event.source_url or "")

    award_amount_raw = payload.get("Award Amount")
    extracted_fields: dict = {
        "company_name": company_name,
        "uei": payload.get("Recipient UEI"),
        "award_amount": str(award_amount_raw) if award_amount_raw is not None else None,
        "naics_code": payload.get("NAICS Code"),
        "naics_description": payload.get("NAICS Description"),
        "action_date": action_date.isoformat(),
        "state_code": payload.get("Place of Performance State Code"),
        "award_type": payload.get("Award Type"),
        "awarding_agency": payload.get("Awarding Agency"),
    }

    content_hash = hashlib.sha256(
        json.dumps(extracted_fields, sort_keys=True, default=str).encode()
    ).hexdigest()

    freshness = _compute_freshness(action_date)

    evidence = EvidenceItem(
        raw_event_id=raw_event.id,
        source_id=raw_event.source_id,
        company_id=None,
        source_url=source_url,
        extracted_fields=extracted_fields,
        content_hash=content_hash,
        claim_supported=_CLAIM_CONTRACT_AWARD,
        confidence_score=_CONFIDENCE_API,
        freshness_score=Decimal(str(freshness)),
    )

    db.add(evidence)
    db.flush()

    log.info("evidence_extracted", award_id=award_id, freshness=freshness)
    return [evidence]
