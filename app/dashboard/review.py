"""
Review helper functions for the Lead Intelligence Dashboard.

All business logic for human review lives here — not in the Streamlit app.
These functions are testable without launching Streamlit.

Founding rule 3: review_decisions is APPEND-ONLY.
  create_review_decision() inserts only. Never updates or deletes.

Auth rule: reviewer_id must come from get_reviewer_id(), never from a form field.
"""
from __future__ import annotations

import os
import uuid
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

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
        .options(joinedload(LeadCandidate.company))
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


def get_award_aggregation(company_id: UUID, db: Session) -> dict:
    """Aggregate CONTRACT_AWARD evidence for a company into summary statistics.

    Reads extracted_fields from evidence_items; skips rows where award_amount or
    action_date is missing/unparseable so partial data never crashes the dashboard.

    Returns a dict with:
        total_amount  – Decimal sum of all valid award amounts
        award_count   – int number of evidence items with a valid award amount
        avg_amount    – Decimal average (0 when award_count == 0)
        by_year       – list of {"year": int, "count": int, "total": Decimal}
                        sorted by year descending
        by_agency     – list of {"agency": str, "count": int, "total": Decimal}
                        sorted by total descending, top 10
        by_action_type – list of {"action_type": str, "count": int, "total": Decimal}
                         sorted by total descending
    """
    stmt = (
        select(EvidenceItem)
        .where(
            EvidenceItem.company_id == company_id,
            EvidenceItem.claim_supported == "CONTRACT_AWARD",
        )
        .order_by(EvidenceItem.captured_at.desc())
    )
    items = list(db.execute(stmt).scalars().all())

    total_amount = Decimal("0")
    award_count = 0
    year_totals: dict[int, dict] = {}
    agency_totals: dict[str, dict] = {}
    action_type_totals: dict[str, dict] = {}

    for item in items:
        fields = item.extracted_fields or {}

        raw_amount = fields.get("award_amount")
        try:
            amount = Decimal(str(raw_amount)) if raw_amount is not None else None
        except (InvalidOperation, TypeError, ValueError):
            amount = None

        if amount is None or amount <= 0:
            continue

        award_count += 1
        total_amount += amount

        action_date_str = fields.get("action_date")
        year: int | None = None
        if action_date_str:
            try:
                year = int(str(action_date_str)[:4])
            except (ValueError, TypeError):
                pass

        if year is not None:
            entry = year_totals.setdefault(year, {"count": 0, "total": Decimal("0")})
            entry["count"] += 1
            entry["total"] += amount

        agency = (fields.get("awarding_agency") or "").strip() or "Unknown"
        a_entry = agency_totals.setdefault(agency, {"count": 0, "total": Decimal("0")})
        a_entry["count"] += 1
        a_entry["total"] += amount

        action_type_raw = fields.get("action_type_description") or fields.get("action_type") or ""
        action_type = action_type_raw.strip() or "Unknown"
        at_entry = action_type_totals.setdefault(action_type, {"count": 0, "total": Decimal("0")})
        at_entry["count"] += 1
        at_entry["total"] += amount

    avg_amount = (total_amount / award_count) if award_count else Decimal("0")

    by_year = sorted(
        [{"year": y, **v} for y, v in year_totals.items()],
        key=lambda r: r["year"],
        reverse=True,
    )
    by_agency = sorted(
        [{"agency": a, **v} for a, v in agency_totals.items()],
        key=lambda r: r["total"],
        reverse=True,
    )[:10]
    by_action_type = sorted(
        [{"action_type": t, **v} for t, v in action_type_totals.items()],
        key=lambda r: r["total"],
        reverse=True,
    )

    return {
        "total_amount": total_amount,
        "award_count": award_count,
        "avg_amount": avg_amount,
        "by_year": by_year,
        "by_agency": by_agency,
        "by_action_type": by_action_type,
    }


def get_award_gate_summary(company_id: UUID, db: Session) -> dict:
    """Gate 10-style award summary for a single company, read from the signals table.

    Exposes the same values Gate 10 evaluates so reviewers can understand
    why a company passed or failed Gate 10. Display-only — never used for
    scoring, gating, tiering, or archiving.

    Only positive award amounts count (zero/negative/null excluded).
    90-day total uses signal_date >= today - 90 days.
    pass_type uses the same env-var thresholds as Gate 10 (default 10 000).

    Keys:
        positive_count   – int
        largest_single   – Decimal (0 if no positive signals)
        recent_total_90d – Decimal (0 if no recent positive signals)
        most_recent_date – date | None
        pass_type        – "single_award_pass" | "aggregate_90d_pass" |
                           "below_threshold" | "unknown"
    """
    _min_single = Decimal(os.getenv("MIN_QUALIFYING_SINGLE_AWARD_AMOUNT", "10000"))
    _min_90d = Decimal(os.getenv("MIN_QUALIFYING_COMPANY_90D_AWARD_TOTAL", "10000"))

    stmt = (
        select(Signal)
        .where(
            Signal.company_id == company_id,
            Signal.signal_type == "CONTRACT_AWARD",
        )
    )
    signals = list(db.execute(stmt).scalars().all())

    cutoff = date.today() - timedelta(days=90)
    largest_single = Decimal("0")
    recent_total_90d = Decimal("0")
    most_recent_date = None
    positive_count = 0

    for sig in signals:
        raw = sig.award_amount
        if raw is None:
            continue
        try:
            amt = Decimal(str(raw))
        except (InvalidOperation, ValueError, TypeError):
            continue
        if amt <= 0:
            continue

        positive_count += 1
        if amt > largest_single:
            largest_single = amt
        if isinstance(sig.signal_date, date) and sig.signal_date >= cutoff:
            recent_total_90d += amt
        if most_recent_date is None or sig.signal_date > most_recent_date:
            most_recent_date = sig.signal_date

    if positive_count == 0:
        pass_type = "unknown"
    elif largest_single >= _min_single:
        pass_type = "single_award_pass"
    elif recent_total_90d >= _min_90d:
        pass_type = "aggregate_90d_pass"
    else:
        pass_type = "below_threshold"

    return {
        "positive_count": positive_count,
        "largest_single": largest_single,
        "recent_total_90d": recent_total_90d,
        "most_recent_date": most_recent_date,
        "pass_type": pass_type,
    }


def format_currency(value) -> str:
    """Return a formatted USD string. None → 'Not available'. Zero → '$0'."""
    if value is None:
        return "Not available"
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return "Not available"
    return f"${float(amount):,.0f}"


def format_date(value) -> str:
    """Return a readable date string. None → 'Not available'."""
    if value is None:
        return "Not available"
    return str(value)
