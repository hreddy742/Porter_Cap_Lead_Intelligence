"""
Review helper functions for the Lead Intelligence Dashboard.

All business logic for human review lives here — not in the Streamlit app.
These functions are testable without launching Streamlit.

Founding rule 3: review_decisions is APPEND-ONLY.
  create_review_decision() inserts only. Never updates or deletes.

Auth rule: reviewer_id must come from get_reviewer_id(), never from a form field.
"""
from __future__ import annotations

import uuid
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Company,
    EvidenceItem,
    LeadCandidate,
    LeadScore,
    ReviewDecision,
    Signal,
)

VALID_ACTIONS = frozenset({
    "approve",
    "reject",
    "mark_duplicate",
    "needs_research",
    "archive",
    "add_note",
})


def get_reviewer_id(headers: dict | None = None) -> str | None:
    """Read reviewer identity from the X-Forwarded-Email header.

    In production, Caddy injects this header after authenticating the user via
    OAuth. The client cannot forge it — the header is stripped by Caddy before
    reaching the Streamlit process.

    Returns None if the header is absent. Callers must handle this by showing
    a warning and disabling review actions rather than falling back to any
    user-supplied value.
    """
    if not headers:
        return None
    return headers.get("X-Forwarded-Email") or None


def create_review_decision(
    lead_candidate_id: UUID,
    action: str,
    note: str | None,
    reviewer_id: str,
    db: Session,
) -> ReviewDecision:
    """Insert a new row into review_decisions. Never modifies existing rows.

    Enforces founding rule 3: review_decisions is append-only. Two contradictory
    decisions on the same lead are both preserved; the most recent decided_at wins
    for display purposes, but the full history is intact.

    Raises:
        ValueError: if reviewer_id is blank or action is not in VALID_ACTIONS.
    """
    if not reviewer_id or not reviewer_id.strip():
        raise ValueError(
            "reviewer_id is required and must come from an authenticated session, "
            "not from a form field."
        )
    if action not in VALID_ACTIONS:
        raise ValueError(
            f"Invalid action {action!r}. Must be one of: {sorted(VALID_ACTIONS)}"
        )

    decision = ReviewDecision(
        id=uuid.uuid4(),
        lead_candidate_id=lead_candidate_id,
        action=action,
        note=note,
        reviewer_id=reviewer_id,
    )
    db.add(decision)
    db.commit()
    return decision


def list_reviewable_leads(db: Session, tier: str | None = None) -> list:
    """Return active lead_candidates joined with their company.

    Optionally filtered by tier (Hot, Warm, Cold, Archive).
    Soft-deleted companies are excluded.
    """
    stmt = (
        select(LeadCandidate)
        .join(Company, LeadCandidate.company_id == Company.id)
        .where(LeadCandidate.status == "active")
        .where(Company.deleted_at.is_(None))
    )
    if tier:
        stmt = stmt.where(LeadCandidate.tier == tier)
    return list(db.execute(stmt).scalars().all())


def get_lead_detail(lead_candidate_id: UUID, db: Session) -> dict | None:
    """Return a dict with all information needed to render the lead detail view.

    Returns None if the lead_candidate_id does not exist.

    Keys: lead, company, latest_score, evidence, signals, review_history.
    review_history is ordered by decided_at ascending (oldest first).
    """
    lead = db.get(LeadCandidate, lead_candidate_id)
    if lead is None:
        return None

    company = db.get(Company, lead.company_id)

    score_stmt = (
        select(LeadScore)
        .where(LeadScore.lead_candidate_id == lead_candidate_id)
        .order_by(LeadScore.computed_at.desc())
        .limit(1)
    )
    latest_score = db.execute(score_stmt).scalars().first()

    evidence_stmt = (
        select(EvidenceItem)
        .where(EvidenceItem.company_id == lead.company_id)
        .order_by(EvidenceItem.captured_at.desc())
    )
    evidence = list(db.execute(evidence_stmt).scalars().all())

    signals_stmt = (
        select(Signal)
        .where(Signal.company_id == lead.company_id)
        .order_by(Signal.signal_date.desc())
    )
    signals = list(db.execute(signals_stmt).scalars().all())

    decisions_stmt = (
        select(ReviewDecision)
        .where(ReviewDecision.lead_candidate_id == lead_candidate_id)
        .order_by(ReviewDecision.decided_at.asc())
    )
    decisions = list(db.execute(decisions_stmt).scalars().all())

    return {
        "lead": lead,
        "company": company,
        "latest_score": latest_score,
        "evidence": evidence,
        "signals": signals,
        "review_history": decisions,
    }


def get_latest_review_action(
    lead_candidate_id: UUID, db: Session
) -> ReviewDecision | None:
    """Return the most recent review_decisions row for this lead, or None."""
    stmt = (
        select(ReviewDecision)
        .where(ReviewDecision.lead_candidate_id == lead_candidate_id)
        .order_by(ReviewDecision.decided_at.desc())
        .limit(1)
    )
    return db.execute(stmt).scalars().first()
