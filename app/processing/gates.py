"""
Mandatory gates — runs BEFORE point scoring (founding rule 4).

evaluate_mandatory_gates() checks all structural and business-rule preconditions
that must hold before a company can enter the scoring queue.

A gated result never receives a numeric score.
This module does NOT calculate scores, create lead_scores, run the scoring
engine, push to Salesforce, or delete anything.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Company, EvidenceItem, LeadCandidate, Signal
from app.processing.suppression import check_suppression

_EXCLUDED_NAICS_PREFIXES = ("52", "61", "92")
_EXCLUDED_INDUSTRY_KEYWORDS = frozenset({"finance", "bank", "lender", "education", "government"})
_SOFT_BLOCK_ROUTES = frozenset({
    "account_review",
    "existing_lead_review",
    "duplicate_review",
    "research_review",
})


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
        for prefix in _EXCLUDED_NAICS_PREFIXES:
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

    # All gates passed — company may proceed to scoring
    return {
        "passed": True,
        "gate_name": None,
        "gate_reason": None,
        "route": "score",
        "should_score": True,
        "suppression": suppression,
    }
