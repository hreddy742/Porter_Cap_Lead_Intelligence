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
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.db.models import (
    Company,
    CompanyContactability,
    EvidenceItem,
    LeadCandidate,
    LeadScore,
    PipelineRun,
    ReviewDecision,
    Signal,
    SourceRegistry,
    SourceRun,
)
from app.ops.health import get_latest_pipeline_health

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


def get_contactability(company_id: UUID, db: Session) -> CompanyContactability | None:
    """Return the company_contactability row for a company, or None if not enriched.

    Read-only display context. This reflects SAM entity validation only — it is
    NOT a verified contact (no confirmed decision-maker email or phone).
    """
    return db.execute(
        select(CompanyContactability).where(
            CompanyContactability.company_id == company_id
        )
    ).scalar_one_or_none()


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


# ─── Pipeline run context ──────────────────────────────────────────────────────

def get_latest_run_context(db: Session) -> dict:
    """Return the latest pipeline run stats for the dashboard header. Read-only.

    Extends get_latest_pipeline_health with leads_scored (sum of tier buckets
    from the pipeline_runs row, which the orchestrator writes at completion).

    Schema gap note: evidence_items_created, companies_resolved, signals_created
    are not written to pipeline_runs or source_runs — returned as None.
    """
    health = get_latest_pipeline_health(db)
    if not health["has_runs"]:
        return {**health, "leads_scored": None}
    run = db.query(PipelineRun).order_by(PipelineRun.started_at.desc()).first()
    leads_scored = None
    if run is not None:
        leads_scored = (
            (run.total_hot or 0)
            + (run.total_warm or 0)
            + (run.total_cold or 0)
            + (run.total_archive or 0)
        )
    return {**health, "leads_scored": leads_scored}


# ─── Source list for filter dropdown ──────────────────────────────────────────

def get_source_list(db: Session) -> list[dict]:
    """Return [{id, name}] for all sources in source_registry, ordered by name."""
    sources = db.execute(
        select(SourceRegistry).order_by(SourceRegistry.name)
    ).scalars().all()
    return [{"id": s.id, "name": s.name} for s in sources]


# ─── Enhanced lead list with view/filter/sort ─────────────────────────────────

def list_leads_filtered(
    db: Session,
    *,
    view: str = "all",
    tier: str | None = None,
    source_id: UUID | None = None,
    sales_status: str | None = None,
    has_contactability: bool | None = None,
    has_evidence_url: bool | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    sort_by: str = "score_desc",
    latest_run_started_at: datetime | None = None,
    include_excluded: bool = False,
    signal_type: str | None = None,
) -> list[dict]:
    """Enhanced lead list with view selector, filters, and sorting.

    view values:
        "all"              — all active leads (default)
        "latest_run"       — leads whose updated_at >= latest_run_started_at
                             (approximate: uses updated_at as proxy for
                              "scored/touched in this run" because
                              lead_candidates has no pipeline_run_id FK)
        "today"            — leads created today UTC
        "recently_updated" — leads updated in the last 7 days
        "custom"           — leads created within [date_from, date_to]

    sort_by values:
        "score_desc"      — highest current_score first (default)
        "newest_first"    — created_at DESC
        "latest_updated"  — updated_at DESC
        "latest_evidence" — latest signal_date DESC (Python sort after DB fetch)
        "largest_award"   — max signal award_amount DESC (Python sort after DB fetch)

    Returns a list of dicts, one per lead:
        lead              — LeadCandidate ORM object (company pre-loaded)
        company           — Company ORM object (same as lead.company)
        primary_source    — source_registry.name for the first source of this company
        latest_signal_date — date | None
        max_award_amount  — Decimal | None
        is_new_in_run     — True if lead.created_at >= latest_run_started_at
                             (approximate: lead_candidates has no first_seen_pipeline_run_id
                              or last_touched_pipeline_run_id FK, so created_at is used as proxy)
    """
    # status='archived' is only ever set on archive-tier leads by the pipeline.
    # Always include both so the full-fetch (no tier param) returns archive leads
    # for client-side filtering, and so tier=archive API calls return all 9.
    stmt = (
        select(LeadCandidate)
        .options(joinedload(LeadCandidate.company))
        .join(Company, LeadCandidate.company_id == Company.id)
        .where(LeadCandidate.status.in_(["active", "archived"]))
        .where(LeadCandidate.sales_status != "suppressed")
        .where(Company.deleted_at.is_(None))
    )

    # ── View filter ───────────────────────────────────────────────────────────
    if view == "today":
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        stmt = stmt.where(LeadCandidate.created_at >= today_start)
    elif view == "recently_updated":
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        stmt = stmt.where(LeadCandidate.updated_at >= cutoff)
    elif view == "latest_run" and latest_run_started_at is not None:
        stmt = stmt.where(LeadCandidate.updated_at >= latest_run_started_at)
    elif view == "custom":
        if date_from is not None:
            dt_from = datetime(
                date_from.year, date_from.month, date_from.day, tzinfo=timezone.utc
            )
            stmt = stmt.where(LeadCandidate.created_at >= dt_from)
        if date_to is not None:
            dt_to = datetime(
                date_to.year, date_to.month, date_to.day, 23, 59, 59, tzinfo=timezone.utc
            )
            stmt = stmt.where(LeadCandidate.created_at <= dt_to)

    # ── Column filters ────────────────────────────────────────────────────────
    if tier:
        stmt = stmt.where(LeadCandidate.tier == tier)
    if sales_status:
        stmt = stmt.where(LeadCandidate.sales_status == sales_status)
    if source_id is not None:
        source_subq = (
            select(EvidenceItem.company_id)
            .where(EvidenceItem.source_id == source_id)
            .where(EvidenceItem.company_id.isnot(None))
        )
        stmt = stmt.where(LeadCandidate.company_id.in_(source_subq))
    if has_contactability is True:
        contact_subq = select(CompanyContactability.company_id)
        stmt = stmt.where(LeadCandidate.company_id.in_(contact_subq))
    elif has_contactability is False:
        contact_subq = select(CompanyContactability.company_id)
        stmt = stmt.where(LeadCandidate.company_id.not_in(contact_subq))
    if has_evidence_url is True:
        url_subq = (
            select(EvidenceItem.company_id)
            .where(EvidenceItem.source_url.isnot(None))
            .where(EvidenceItem.company_id.isnot(None))
        )
        stmt = stmt.where(LeadCandidate.company_id.in_(url_subq))

    if not include_excluded:
        stmt = stmt.where(
            (LeadCandidate.sector_excluded == False)  # noqa: E712
            | LeadCandidate.sector_excluded.is_(None)
        )

    if signal_type:
        sig_subq = select(Signal.company_id).where(Signal.signal_type == signal_type)
        stmt = stmt.where(LeadCandidate.company_id.in_(sig_subq))

    # ── SQL-level sorting (for non-signal sorts) ──────────────────────────────
    if sort_by == "newest_first":
        stmt = stmt.order_by(LeadCandidate.created_at.desc())
    elif sort_by == "latest_updated":
        stmt = stmt.order_by(LeadCandidate.updated_at.desc())
    else:
        stmt = stmt.order_by(LeadCandidate.current_score.desc().nullslast())

    leads = list(db.execute(stmt).scalars().all())

    if not leads:
        return []

    company_ids = [lead.company_id for lead in leads]

    # ── Bulk: signal aggregates per company ───────────────────────────────────
    sig_rows = db.execute(
        select(
            Signal.company_id,
            func.max(Signal.signal_date).label("latest_signal_date"),
            func.max(Signal.award_amount).label("max_award"),
        )
        .where(Signal.company_id.in_(company_ids))
        .group_by(Signal.company_id)
    ).all()
    latest_signal_date_by_company: dict = {
        row.company_id: row.latest_signal_date for row in sig_rows
    }
    max_award_by_company: dict = {
        row.company_id: (
            Decimal(str(row.max_award)) if row.max_award is not None else None
        )
        for row in sig_rows
    }

    # ── Bulk: primary source per company ──────────────────────────────────────
    primary_source_by_company = _get_primary_source_by_company(company_ids, db)

    # ── Python-level sorting for signal-based sorts ───────────────────────────
    if sort_by == "latest_evidence":
        leads = sorted(
            leads,
            key=lambda lc: latest_signal_date_by_company.get(lc.company_id) or date.min,
            reverse=True,
        )
    elif sort_by == "largest_award":
        leads = sorted(
            leads,
            key=lambda lc: max_award_by_company.get(lc.company_id) or Decimal("0"),
            reverse=True,
        )

    # ── Assemble result rows ──────────────────────────────────────────────────
    rows = []
    for lead in leads:
        cid = lead.company_id
        rows.append({
            "lead": lead,
            "company": lead.company,
            "primary_source": primary_source_by_company.get(cid),
            "latest_signal_date": latest_signal_date_by_company.get(cid),
            "max_award_amount": max_award_by_company.get(cid),
            "is_new_in_run": (
                latest_run_started_at is not None
                and lead.created_at is not None
                and lead.created_at >= latest_run_started_at
            ),
        })
    return rows


def _get_primary_source_by_company(company_ids: list, db: Session) -> dict:
    """Return {company_id: source_name} for each company's first source."""
    if not company_ids:
        return {}
    rows = db.execute(
        select(
            EvidenceItem.company_id,
            SourceRegistry.name.label("source_name"),
        )
        .join(SourceRegistry, EvidenceItem.source_id == SourceRegistry.id)
        .where(EvidenceItem.company_id.in_(company_ids))
        .group_by(EvidenceItem.company_id, SourceRegistry.name)
    ).all()
    result: dict = {}
    for row in rows:
        if row.company_id not in result:
            result[row.company_id] = row.source_name
    return result
