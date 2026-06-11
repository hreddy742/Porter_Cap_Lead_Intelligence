"""
Gate 10 controlled reprocessing service — Phase 2A.

Evaluates existing active lead_candidates against Gate 10 (award amount quality
gate) and optionally archives those that now fail.

This module never commits — the caller is responsible for db.commit() or
db.rollback() so that the full reprocess runs in a single transaction.

Active lead_candidates were all created before Gate 10 was introduced; they
have gate_result='passed' and status='active'.  Retroactive application of
Gate 10 sets status='archived' on leads that would fail, removing them from
the dashboard active queue and the impact-report query.

Fields updated for failing leads (apply mode only):
    status        = 'archived'
    gate_result   = 'archived'
    gate_reason   = 'award_amount_too_small'
    tier          = 'archive'
    current_score = NULL

Fields never touched:
    lead_scores rows, companies, signals, evidence_items, raw_source_events,
    sales_status, deleted_at, created_at
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from sqlalchemy import text, update
from sqlalchemy.orm import Session

from app.db.models import LeadCandidate, ReviewDecision
from app.ops.gate_impact import classify_gate10

_DEFAULT_MIN_SINGLE = Decimal("10000")
_DEFAULT_MIN_90D = Decimal("10000")

_SYSTEM_REVIEWER = "system:gate10_reprocess"
_ARCHIVE_ACTION = "archive"
_ARCHIVE_NOTE = "Gate 10 reprocessing: award_amount_too_small"


def _parse_env_decimal(name: str, default: Decimal) -> Decimal:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return Decimal(raw.strip())
    except (InvalidOperation, ValueError):
        raise ValueError(
            f"Configuration error: {name}={raw!r} is not a valid decimal. "
            f"Unset it to use the default ({default})."
        )


@dataclass
class Gate10ReprocessResult:
    dry_run: bool
    total_active: int
    passing: list[dict] = field(default_factory=list)      # includes pass_only_90d
    failing: list[dict] = field(default_factory=list)      # fail_too_small only
    pass_only_90d: list[dict] = field(default_factory=list)  # subset of passing
    applied: int = 0   # 0 in dry-run; count of updated rows in apply mode


# Mirrors gate_impact._IMPACT_QUERY exactly — duplicated here to avoid importing
# a private symbol from a sibling module.
# Filters: status='active' and deleted_at IS NULL on both lead_candidates and companies.
_REPROCESS_QUERY = text("""
    SELECT
      lc.id                                           AS lead_candidate_id,
      lc.company_id,
      lc.tier,
      c.canonical_name,
      COALESCE(
        MAX(CASE WHEN s.award_amount > 0 THEN s.award_amount END),
        0
      )                                               AS largest_single,
      COALESCE(
        SUM(CASE WHEN s.award_amount > 0
                  AND s.signal_date >= CURRENT_DATE - INTERVAL '90 days'
                  THEN s.award_amount
             END),
        0
      )                                               AS recent_total_90d,
      COUNT(CASE WHEN s.award_amount > 0 THEN 1 END) AS positive_award_count,
      MAX(CASE WHEN s.award_amount > 0 THEN s.signal_date END)
                                                      AS most_recent_award_date
    FROM lead_candidates lc
    JOIN  companies c ON c.id = lc.company_id
    LEFT JOIN signals s ON s.company_id = lc.company_id
    WHERE lc.deleted_at IS NULL
      AND lc.status     = 'active'
      AND c.deleted_at  IS NULL
    GROUP BY lc.id, lc.company_id, lc.tier, c.canonical_name
""")


def run_gate10_reprocess(db: Session, dry_run: bool = True) -> Gate10ReprocessResult:
    """
    Evaluate active lead_candidates against Gate 10 and optionally archive failures.

    dry_run=True  (default): reads only — no writes, no commit.
    dry_run=False           : issues one batch UPDATE on lead_candidates and one
                              ReviewDecision INSERT per failing lead.
                              Does NOT commit — caller must commit or rollback.

    Only candidates with status='active' and deleted_at IS NULL are considered.
    Passing candidates (including pass_only_90d) are never modified.
    """
    min_single = _parse_env_decimal(
        "MIN_QUALIFYING_SINGLE_AWARD_AMOUNT", _DEFAULT_MIN_SINGLE
    )
    min_90d = _parse_env_decimal(
        "MIN_QUALIFYING_COMPANY_90D_AWARD_TOTAL", _DEFAULT_MIN_90D
    )

    rows = db.execute(_REPROCESS_QUERY).fetchall()

    passing: list[dict] = []
    failing: list[dict] = []
    pass_only_90d: list[dict] = []

    for row in rows:
        tier = row.tier or "unscored"
        largest = Decimal(str(row.largest_single))
        recent = Decimal(str(row.recent_total_90d))
        verdict = classify_gate10(largest, recent, min_single, min_90d)

        entry = {
            "lead_candidate_id": row.lead_candidate_id,
            "canonical_name": row.canonical_name,
            "tier_before": tier,
            "largest_single": float(largest),
            "recent_total_90d": float(recent),
            "positive_award_count": int(row.positive_award_count),
            "most_recent_award_date": (
                str(row.most_recent_award_date) if row.most_recent_award_date else None
            ),
        }

        if verdict == "fail_too_small":
            failing.append(entry)
        else:
            passing.append(entry)
            if verdict == "pass_only_90d":
                pass_only_90d.append(entry)

    result = Gate10ReprocessResult(
        dry_run=dry_run,
        total_active=len(rows),
        passing=passing,
        failing=failing,
        pass_only_90d=pass_only_90d,
        applied=0,
    )

    if dry_run or not failing:
        return result

    # Apply mode — one batch UPDATE + one ReviewDecision per failing lead.
    # No commit here; the caller (script) commits after verifying the result.
    failing_ids = [entry["lead_candidate_id"] for entry in failing]

    db.execute(
        update(LeadCandidate)
        .where(LeadCandidate.id.in_(failing_ids))
        .values(
            status="archived",
            gate_result="archived",
            gate_reason="award_amount_too_small",
            tier="archive",
            current_score=None,
        )
    )

    for entry in failing:
        db.add(
            ReviewDecision(
                id=uuid.uuid4(),
                lead_candidate_id=entry["lead_candidate_id"],
                reviewer_id=_SYSTEM_REVIEWER,
                action=_ARCHIVE_ACTION,
                note=_ARCHIVE_NOTE,
            )
        )

    result.applied = len(failing)
    return result
