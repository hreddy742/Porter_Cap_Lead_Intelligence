"""
CSV export for approved leads — Task 10.

Export-eligible: active lead_candidates whose latest review_decision.action == 'approve'.
Read-only: no INSERT, UPDATE, or DELETE on any table.
No Salesforce push. No AI. No cron.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Company,
    EvidenceItem,
    LeadCandidate,
    LeadScore,
    RawSourceEvent,
    ReviewDecision,
    Signal,
)

CSV_FIELDNAMES = [
    "lead_candidate_id",
    "company_id",
    "company_name",
    "tier",
    "current_score",
    "sales_status",
    "state",
    "website_domain",
    "naics_code",
    "industry",
    "latest_review_action",
    "latest_review_decided_at",
    "reviewer_id",
    "score_total",
    "score_tier",
    "component_breakdown",
    "evidence_urls",
    "signal_types",
    "source_record_ids",
]


def get_latest_review_decision(
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


def is_export_eligible(latest_decision: ReviewDecision | None) -> bool:
    """True only when the most recent decision action is 'approve'."""
    if latest_decision is None:
        return False
    return latest_decision.action == "approve"


def build_export_row(
    lead: LeadCandidate,
    company: Company,
    latest_decision: ReviewDecision,
    score: LeadScore | None,
    evidence_items: list,
    signals: list,
    source_record_ids: list[str],
) -> dict:
    """Build a flat dict for one CSV row. Pure function; no DB access."""
    evidence_urls = [e.source_url for e in evidence_items if e.source_url]
    signal_types = sorted({s.signal_type for s in signals if s.signal_type})

    return {
        "lead_candidate_id": str(lead.id),
        "company_id": str(company.id),
        "company_name": company.canonical_name or "",
        "tier": lead.tier or "",
        "current_score": lead.current_score if lead.current_score is not None else "",
        "sales_status": lead.sales_status or "",
        "state": company.state or "",
        "website_domain": company.website_domain or "",
        "naics_code": company.naics_code or "",
        "industry": company.industry or "",
        "latest_review_action": latest_decision.action,
        "latest_review_decided_at": str(latest_decision.decided_at),
        "reviewer_id": latest_decision.reviewer_id,
        "score_total": score.total_score if score is not None else "",
        "score_tier": score.tier if score is not None else "",
        "component_breakdown": json.dumps(score.component_breakdown) if score is not None else "",
        "evidence_urls": "|".join(evidence_urls),
        "signal_types": "|".join(signal_types),
        "source_record_ids": "|".join(source_record_ids),
    }


def export_approved_leads_csv(output_path: str, db: Session) -> dict:
    """
    Write a CSV of approved leads to output_path.

    Only active lead_candidates whose latest review_decision.action == 'approve'
    are included. Leads scored without a lead_scores row are included if approved;
    score columns will be empty strings in that case.

    Returns:
        {"exported_count": int, "output_path": str, "skipped_count": int}

    Side effects: writes one CSV file at output_path. Never writes to the database.
    """
    # ── 1: load all active lead_candidates ────────────────────────────────────
    leads_stmt = (
        select(LeadCandidate)
        .where(LeadCandidate.status == "active")
        .where(LeadCandidate.deleted_at.is_(None))
        .order_by(LeadCandidate.created_at.asc())
    )
    leads: list[LeadCandidate] = list(db.execute(leads_stmt).scalars().all())

    rows: list[dict] = []
    exported_count = 0
    skipped_count = 0

    for lead in leads:
        # ── 2: check eligibility via latest decision ───────────────────────────
        latest_decision = get_latest_review_decision(lead.id, db)
        if not is_export_eligible(latest_decision):
            skipped_count += 1
            continue

        # ── 3: company ────────────────────────────────────────────────────────
        company: Company = db.get(Company, lead.company_id)

        # ── 4: latest score (optional — lead may not have been scored yet) ────
        score_stmt = (
            select(LeadScore)
            .where(LeadScore.lead_candidate_id == lead.id)
            .order_by(LeadScore.computed_at.desc())
            .limit(1)
        )
        score: LeadScore | None = db.execute(score_stmt).scalars().first()

        # ── 5: evidence items ─────────────────────────────────────────────────
        evidence_stmt = (
            select(EvidenceItem)
            .where(EvidenceItem.company_id == lead.company_id)
        )
        evidence_items: list[EvidenceItem] = list(
            db.execute(evidence_stmt).scalars().all()
        )

        # ── 6: signals ────────────────────────────────────────────────────────
        signals_stmt = (
            select(Signal)
            .where(Signal.company_id == lead.company_id)
        )
        signals: list[Signal] = list(db.execute(signals_stmt).scalars().all())

        # ── 7: source_record_ids via raw_source_events ────────────────────────
        source_record_ids: list[str] = []
        raw_event_ids = [e.raw_event_id for e in evidence_items if e.raw_event_id]
        if raw_event_ids:
            raw_stmt = (
                select(RawSourceEvent)
                .where(RawSourceEvent.id.in_(raw_event_ids))
            )
            raw_events = list(db.execute(raw_stmt).scalars().all())
            source_record_ids = [
                r.source_record_id for r in raw_events if r.source_record_id
            ]

        rows.append(
            build_export_row(
                lead=lead,
                company=company,
                latest_decision=latest_decision,
                score=score,
                evidence_items=evidence_items,
                signals=signals,
                source_record_ids=source_record_ids,
            )
        )
        exported_count += 1

    # ── 8: write CSV ──────────────────────────────────────────────────────────
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    return {
        "exported_count": exported_count,
        "output_path": output_path,
        "skipped_count": skipped_count,
    }
