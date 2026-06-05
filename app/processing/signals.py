"""
Signal detection — Task 04.

Reads an evidence_items row and writes one signals row for CONTRACT_AWARD evidence
that has been company-resolved (company_id is not NULL).

Quarantine conditions (return [] without raising):
  - evidence row does not exist
  - evidence.company_id is NULL (resolution must run first)
  - claim_supported != CONTRACT_AWARD
  - action_date is missing or unparseable

Deduplication: if a signal already exists for this evidence_id, return it without
creating a second row.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import EvidenceItem, Signal

logger = structlog.get_logger(__name__)

_CLAIM_CONTRACT_AWARD = "CONTRACT_AWARD"


def classify_signal_strength(award_amount: Decimal | float | None) -> str:
    """Classify award_amount into a signal_strength tier.

    >= 1_000_000 → "strong"
    >= 250_000   → "medium"
    < 250_000    → "weak"
    """
    if award_amount is None:
        return "weak"
    try:
        amount = Decimal(str(award_amount))
    except InvalidOperation:
        return "weak"
    if amount >= Decimal("1000000"):
        return "strong"
    if amount >= Decimal("250000"):
        return "medium"
    return "weak"


def _parse_date(value: object) -> date | None:
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


def _parse_amount(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def detect_signals_for_evidence(evidence_id: UUID, db: Session) -> list[Signal]:
    """
    Detect and persist signals for one evidence_items row.

    Adds one Signal to the session and flushes.
    Returns [] and logs a warning for any quarantine condition.
    Returns existing signals without creating duplicates if called twice.
    Never raises.
    """
    evidence: EvidenceItem | None = db.get(EvidenceItem, evidence_id)
    if evidence is None:
        logger.warning("signal_evidence_not_found", evidence_id=str(evidence_id))
        return []

    log = logger.bind(evidence_id=str(evidence_id))

    if evidence.company_id is None:
        log.warning("signal_skipped_company_id_null")
        return []

    if evidence.claim_supported != _CLAIM_CONTRACT_AWARD:
        log.warning(
            "signal_skipped_wrong_claim",
            claim_supported=evidence.claim_supported,
        )
        return []

    fields: dict = evidence.extracted_fields or {}
    signal_date = _parse_date(fields.get("action_date"))
    if signal_date is None:
        log.warning("signal_skipped_bad_action_date", value=fields.get("action_date"))
        return []

    existing = db.execute(
        select(Signal).where(Signal.evidence_id == evidence_id)
    ).scalars().all()
    if existing:
        log.info("signal_already_exists", count=len(existing))
        return list(existing)

    award_amount = _parse_amount(fields.get("award_amount"))
    strength = classify_signal_strength(award_amount)

    signal = Signal(
        company_id=evidence.company_id,
        source_id=evidence.source_id,
        evidence_id=evidence_id,
        signal_type=_CLAIM_CONTRACT_AWARD,
        signal_date=signal_date,
        signal_strength=strength,
        freshness_score=evidence.freshness_score,
        award_amount=award_amount,
    )

    db.add(signal)
    db.flush()

    log.info(
        "signal_created",
        signal_type=_CLAIM_CONTRACT_AWARD,
        signal_strength=strength,
        award_amount=str(award_amount),
    )
    return [signal]
