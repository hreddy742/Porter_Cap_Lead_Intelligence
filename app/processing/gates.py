"""
Mandatory gates — runs BEFORE point scoring (founding rule 4).

evaluate_mandatory_gates() checks all structural and business-rule preconditions
that must hold before a company can enter the scoring queue.

A gated result never receives a numeric score.
This module does NOT calculate scores, create lead_scores, run the scoring
engine, push to Salesforce, or delete anything.
"""
from __future__ import annotations

import os
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Company, EvidenceItem, LeadCandidate, Signal
from app.processing.suppression import check_suppression

logger = structlog.get_logger(__name__)

# Prefixes used by the hard-block gate (Gate 4 — excluded_industry).
_HARD_BLOCK_NAICS_PREFIXES = ("52", "61", "92")

# Prefixes for the soft-flag (Phase 2B ICP policy, confirmed by John Cox Miller June 24 2026).
# Leads in these sectors are scored and stored normally but hidden from sales by default.
_EXCLUDED_NAICS_PREFIXES: frozenset[str] = frozenset({
    "11",  # Agriculture
    "22",  # Utilities
    "23",  # Construction
    "52",  # Finance and Insurance
    "61",  # Educational Services
    "62",  # Health Care and Social Assistance
    "71",  # Arts, Entertainment, Recreation
    "92",  # Public Administration
    # Source: John Cox Miller, Porter Capital, June 24 2026
})

_EXCLUDED_INDUSTRY_KEYWORDS = frozenset({"finance", "bank", "lender", "education", "government"})
_SOFT_BLOCK_ROUTES = frozenset({
    "account_review",
    "existing_lead_review",
    "duplicate_review",
    "research_review",
})


def _parse_env_decimal(name: str, default: Decimal) -> Decimal:
    """Read an env var and parse it as Decimal.

    Returns default if the variable is unset.
    Raises ValueError with a clear message if the value is set but not a valid decimal,
    so misconfiguration is caught at call time rather than silently producing wrong results.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return Decimal(raw.strip())
    except (InvalidOperation, ValueError):
        raise ValueError(
            f"Configuration error: {name}={raw!r} is not a valid decimal number. "
            f"Fix the environment variable or unset it to use the default ({default})."
        )


def _parse_signal_amount(value) -> Decimal | None:
    """Return value as a positive Decimal, or None if non-positive or unparseable."""
    if value is None:
        return None
    try:
        d = Decimal(str(value))
        return d if d > 0 else None
    except (InvalidOperation, ValueError):
        return None


def _gated(gate_reason: str, route: str) -> dict:
    return {
        "passed": False,
        "gate_name": gate_reason,
        "gate_reason": gate_reason,
        "route": route,
        "should_score": False,
        "suppression": None,
    }


def _is_excluded_industry(company: Company) -> bool:
    naics = company.naics_code
    if naics:
        for prefix in _HARD_BLOCK_NAICS_PREFIXES:
            if str(naics).startswith(prefix):
                return True
    industry = (company.industry or "").lower()
    return any(kw in industry for kw in _EXCLUDED_INDUSTRY_KEYWORDS)


def evaluate_mandatory_gates(company_id: UUID, db: Session) -> dict:
    """
    Run all mandatory gates for company_id.

    Returns a dict with keys:
        passed        bool
        gate_name     str | None   — which gate fired (None if all passed)
        gate_reason   str | None   — machine-readable reason code
        route         str | None   — where this company should go next
        should_score  bool         — always False when passed=False
        suppression   dict | None  — raw check_suppression result (None for pre-suppression gates)

    Gates run in order; first failure wins.
    """
    company: Company | None = db.get(Company, company_id)
    if company is None:
        return _gated("company_not_found", "archive")

    # Gate 1 — no evidence
    evidence_items = (
        db.execute(select(EvidenceItem).where(EvidenceItem.company_id == company_id))
        .scalars()
        .all()
    )
    if not evidence_items:
        return _gated("no_evidence", "archive")

    # Gate 2 — no signal
    signals = (
        db.execute(select(Signal).where(Signal.company_id == company_id))
        .scalars()
        .all()
    )
    if not signals:
        return _gated("no_signal", "archive")

    # Gate 3 — stale signal (every signal is below the freshness floor)
    if all(float(s.freshness_score) < 0.1 for s in signals):
        return _gated("stale_signal", "archive")

    # Gate 4 — excluded industry
    if _is_excluded_industry(company):
        return _gated("excluded_industry", "archive")

    # Gate 5 — non-US company
    if company.country and company.country != "US":
        return _gated("non_us", "archive")

    # Gate 6 — clearly B2C
    if company.business_type == "b2c":
        return _gated("b2c", "archive")

    # Gates 7 & 8 — suppression (hard block first, then soft routes)
    suppression = check_suppression(company_id, db)

    if suppression["route"] == "hard_block":
        return {
            "passed": False,
            "gate_name": "suppression_hard_block",
            "gate_reason": suppression["reason"],
            "route": "hard_block",
            "should_score": False,
            "suppression": suppression,
        }

    if suppression["route"] in _SOFT_BLOCK_ROUTES:
        return {
            "passed": False,
            "gate_name": "suppression_route",
            "gate_reason": suppression["reason"],
            "route": suppression["route"],
            "should_score": False,
            "suppression": suppression,
        }

    # Gate 9 — duplicate active lead candidate already exists for this company
    active_leads = (
        db.execute(
            select(LeadCandidate).where(
                LeadCandidate.company_id == company_id,
                LeadCandidate.status == "active",
                LeadCandidate.deleted_at.is_(None),
            )
        )
        .scalars()
        .all()
    )
    if active_leads:
        return {
            "passed": False,
            "gate_name": "duplicate_active",
            "gate_reason": "duplicate_active",
            "route": "existing_lead_review",
            "should_score": False,
            "suppression": suppression,
        }

    # Gate 10 — award amount quality gate
    # Only positive award amounts count. A company passes if:
    #   (a) its largest single positive award >= MIN_QUALIFYING_SINGLE_AWARD_AMOUNT, OR
    #   (b) its total positive awards in the last 90 days >= MIN_QUALIFYING_COMPANY_90D_AWARD_TOTAL.
    # Negative and zero amounts never contribute to qualification.
    _min_single = _parse_env_decimal(
        "MIN_QUALIFYING_SINGLE_AWARD_AMOUNT", Decimal("10000")
    )
    _min_90d = _parse_env_decimal(
        "MIN_QUALIFYING_COMPANY_90D_AWARD_TOTAL", Decimal("10000")
    )
    cutoff = date.today() - timedelta(days=90)
    largest_single = Decimal("0")
    recent_total = Decimal("0")
    for s in signals:
        amt = _parse_signal_amount(s.award_amount)
        if amt is None:
            continue
        if amt > largest_single:
            largest_single = amt
        if isinstance(s.signal_date, date) and s.signal_date >= cutoff:
            recent_total += amt
    if largest_single < _min_single and recent_total < _min_90d:
        return _gated("award_amount_too_small", "archive")

    # All gates passed — company may proceed to scoring
    return {
        "passed": True,
        "gate_name": None,
        "gate_reason": None,
        "route": "score",
        "should_score": True,
        "suppression": suppression,
    }


def flag_excluded_sector(
    company: Company,
    lead_candidate: LeadCandidate,
    db: Session,
) -> bool:
    """
    Soft-flags a lead if its NAICS falls in an excluded sector per Porter ICP policy.

    Does NOT block the lead. Lead still gets scored and stored normally.
    Returns True if flagged, False if not.
    Only flags if company.naics_code is not None.
    """
    if not company.naics_code:
        return False
    for prefix in _EXCLUDED_NAICS_PREFIXES:
        if company.naics_code.startswith(prefix):
            lead_candidate.sector_excluded = True
            lead_candidate.sector_excluded_reason = (
                f"NAICS {company.naics_code} is in excluded "
                f"sector {prefix} per Porter ICP policy "
                f"confirmed by John Cox Miller June 24 2026"
            )
            db.flush()
            logger.info(
                "lead_sector_excluded_flagged",
                company=company.canonical_name,
                naics=company.naics_code,
                prefix=prefix,
            )
            return True
    return False
