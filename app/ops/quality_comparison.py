"""
Phase 2A Before/After Quality Comparison module.

'Before' state: baseline constants captured from the Phase 2A Gate 10
impact report BEFORE run_gate10_reprocess(dry_run=False) was applied.
These values cannot be reconstructed from the database after reprocessing.

'After' state: computed live from the current database.

This module is read-only — no writes, no scoring, no gate changes.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.ops.gate_impact import build_gate10_impact_report

# ── Before-state constants ─────────────────────────────────────────────────────
# Source: Phase 2A Gate 10 impact report — baseline captured before reprocessing.
# Label: "Baseline captured before Gate 10 reprocessing."
# These cannot be reconstructed from the database after Gate 10 reprocessing
# archived 8 leads (status changed from 'active' to 'archived').

BEFORE_ACTIVE_LEAD_CANDIDATES = 39
BEFORE_WARM = 35
BEFORE_COLD = 4
BEFORE_GATE10_FAILING = 8        # fail verdict: award_amount_too_small
BEFORE_PASS_ONLY_90D = 1         # pass verdict: only 90-day aggregate qualifies
BEFORE_ARCHIVED_BY_GATE10 = 0    # none archived before reprocessing ran
# Derived to keep arithmetic consistent: passing_single + failing + pass_only_90d == total
BEFORE_GATE10_PASSING_SINGLE = (
    BEFORE_ACTIVE_LEAD_CANDIDATES - BEFORE_GATE10_FAILING - BEFORE_PASS_ONLY_90D
)  # = 30


# ── Interpretation notes ───────────────────────────────────────────────────────
# Honest observations about what Phase 2A achieved.
# Explicit caution: none of these statements prove ROI, sales readiness,
# factoring need, or closed-won conversion.

INTERPRETATION_NOTES = [
    "Phase 2A reduced active Gate 10 failing leads from 8 to 0.",
    "Active reviewable leads dropped from 39 to 31.",
    "The active list is cleaner under the approved Gate 10 rule.",
    "This report does not prove ROI.",
    "This report does not prove sales readiness.",
    "This report does not prove factoring need.",
    "This report does not prove closed-won conversion or contactability.",
]


# ── Dataclasses ────────────────────────────────────────────────────────────────

@dataclass
class BeforeState:
    """Baseline captured before Gate 10 reprocessing — hardcoded constants only."""
    active_lead_candidates: int = BEFORE_ACTIVE_LEAD_CANDIDATES
    warm: int = BEFORE_WARM
    cold: int = BEFORE_COLD
    gate10_failing: int = BEFORE_GATE10_FAILING
    pass_only_90d: int = BEFORE_PASS_ONLY_90D
    gate10_passing_single: int = BEFORE_GATE10_PASSING_SINGLE
    archived_by_gate10: int = BEFORE_ARCHIVED_BY_GATE10


@dataclass
class AfterSnapshot:
    """Live database state computed after Gate 10 reprocessing."""
    active_lead_candidates: int
    warm: int
    cold: int
    gate10_failing: int
    pass_only_90d: int
    gate10_passing_single: int
    archived_by_gate10: int
    archived_company_names: list = field(default_factory=list)


@dataclass
class ComparisonReport:
    before: BeforeState
    after: AfterSnapshot


# ── DB query functions ─────────────────────────────────────────────────────────

def count_gate10_archived(db: Session) -> int:
    """Count review_decisions rows written by Gate 10 reprocessing."""
    return (
        db.execute(
            text(
                "SELECT COUNT(*) FROM review_decisions "
                "WHERE reviewer_id = 'system:gate10_reprocess' AND action = 'archive'"
            )
        ).scalar()
        or 0
    )


def get_gate10_archived_names(db: Session) -> list[str]:
    """Return distinct company names archived by Gate 10 reprocessing, sorted."""
    rows = db.execute(
        text("""
            SELECT DISTINCT c.canonical_name
            FROM review_decisions rd
            JOIN lead_candidates lc ON lc.id = rd.lead_candidate_id
            JOIN companies c ON c.id = lc.company_id
            WHERE rd.reviewer_id = 'system:gate10_reprocess'
              AND rd.action = 'archive'
            ORDER BY c.canonical_name
        """)
    ).fetchall()
    return [row.canonical_name for row in rows]


# ── Report builders ────────────────────────────────────────────────────────────

def build_after_snapshot(db: Session) -> AfterSnapshot:
    """Compute current active lead quality metrics from the database."""
    impact = build_gate10_impact_report(db)
    tier_counts = impact.tier_counts_before  # tier distribution of current active leads

    archived_count = count_gate10_archived(db)
    archived_names = get_gate10_archived_names(db)

    passing_single = impact.total_pass - impact.total_pass_only_90d

    return AfterSnapshot(
        active_lead_candidates=impact.total_lead_candidates,
        warm=tier_counts.get("warm", 0),
        cold=tier_counts.get("cold", 0),
        gate10_failing=impact.total_fail,
        pass_only_90d=impact.total_pass_only_90d,
        gate10_passing_single=passing_single,
        archived_by_gate10=archived_count,
        archived_company_names=archived_names,
    )


def build_comparison(db: Session) -> ComparisonReport:
    return ComparisonReport(
        before=BeforeState(),
        after=build_after_snapshot(db),
    )


def compute_changes(report: ComparisonReport) -> dict[str, int]:
    """Return change (after - before) for each comparable metric. Pure function."""
    b = report.before
    a = report.after
    return {
        "active_lead_candidates": a.active_lead_candidates - b.active_lead_candidates,
        "warm": a.warm - b.warm,
        "cold": a.cold - b.cold,
        "gate10_failing": a.gate10_failing - b.gate10_failing,
        "pass_only_90d": a.pass_only_90d - b.pass_only_90d,
        "gate10_passing_single": a.gate10_passing_single - b.gate10_passing_single,
        "archived_by_gate10": a.archived_by_gate10 - b.archived_by_gate10,
    }
