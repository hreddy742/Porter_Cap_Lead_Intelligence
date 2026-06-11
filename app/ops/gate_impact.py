"""
Read-only Gate 10 Before/After Impact Report.

Computes how existing active lead_candidates would be affected by Gate 10
(award amount quality gate) if evaluated now.  Never writes to the database.

Gate 10 logic mirrored exactly from app/processing/gates.py:
  - largest_single  = MAX positive award across ALL signals for the company
  - recent_total_90d = SUM positive awards where signal_date >= today - 90 days
  - pass  if largest_single >= MIN_QUALIFYING_SINGLE_AWARD_AMOUNT
  - OR       recent_total_90d >= MIN_QUALIFYING_COMPANY_90D_AWARD_TOTAL
  - fail  if both are below threshold
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from sqlalchemy import text
from sqlalchemy.orm import Session

_DEFAULT_MIN_SINGLE = Decimal("10000")
_DEFAULT_MIN_90D = Decimal("10000")


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


def classify_gate10(
    largest_single: Decimal,
    recent_total: Decimal,
    min_single: Decimal,
    min_90d: Decimal,
) -> str:
    """
    Classify Gate 10 outcome given pre-aggregated award amounts.

    Mirrors gates.py Gate 10 logic exactly — no new rules introduced.

    Returns:
        'pass'           — largest single award >= min_single (qualifies outright)
        'pass_only_90d'  — only the 90-day total qualifies (single too small)
        'fail_too_small' — neither threshold met
    """
    if largest_single >= min_single:
        return "pass"
    if recent_total >= min_90d:
        return "pass_only_90d"
    return "fail_too_small"


@dataclass
class Gate10ImpactReport:
    total_lead_candidates: int
    tier_counts_before: dict[str, int] = field(default_factory=dict)
    total_pass: int = 0
    total_fail: int = 0
    tier_breakdown: dict[str, dict[str, int]] = field(default_factory=dict)
    total_pass_only_90d: int = 0
    top_failing: list[dict] = field(default_factory=list)
    top_pass_only_90d: list[dict] = field(default_factory=list)
    min_single_threshold: Decimal = field(default_factory=lambda: _DEFAULT_MIN_SINGLE)
    min_90d_threshold: Decimal = field(default_factory=lambda: _DEFAULT_MIN_90D)


# One query: LEFT JOIN aggregates signal amounts per lead candidate.
# The SQL mirrors gates.py exactly:
#   largest_single  = MAX(positive award amounts) across all signals, no date filter
#   recent_total_90d = SUM(positive award amounts) where signal_date >= today - 90 days
# Zero and negative amounts are excluded via CASE WHEN award_amount > 0.
_IMPACT_QUERY = text("""
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


def build_gate10_impact_report(db: Session) -> Gate10ImpactReport:
    """
    Build a read-only Gate 10 impact report for all active lead candidates.

    Reads: lead_candidates, companies, signals.
    Writes: nothing.
    """
    min_single = _parse_env_decimal("MIN_QUALIFYING_SINGLE_AWARD_AMOUNT", _DEFAULT_MIN_SINGLE)
    min_90d = _parse_env_decimal("MIN_QUALIFYING_COMPANY_90D_AWARD_TOTAL", _DEFAULT_MIN_90D)

    rows = db.execute(_IMPACT_QUERY).fetchall()

    report = Gate10ImpactReport(
        total_lead_candidates=len(rows),
        min_single_threshold=min_single,
        min_90d_threshold=min_90d,
    )

    tier_pass: dict[str, int] = {}
    tier_fail: dict[str, int] = {}
    failing: list[dict] = []
    pass_only_90d_list: list[dict] = []

    for row in rows:
        tier = row.tier or "unscored"
        largest = Decimal(str(row.largest_single))
        recent = Decimal(str(row.recent_total_90d))
        verdict = classify_gate10(largest, recent, min_single, min_90d)

        report.tier_counts_before[tier] = report.tier_counts_before.get(tier, 0) + 1

        entry = {
            "canonical_name": row.canonical_name,
            "tier": tier,
            "largest_single": float(largest),
            "recent_total_90d": float(recent),
            "positive_award_count": int(row.positive_award_count),
            "most_recent_award_date": (
                str(row.most_recent_award_date) if row.most_recent_award_date else None
            ),
        }

        if verdict in ("pass", "pass_only_90d"):
            report.total_pass += 1
            tier_pass[tier] = tier_pass.get(tier, 0) + 1
            if verdict == "pass_only_90d":
                pass_only_90d_list.append(entry)
        else:
            report.total_fail += 1
            tier_fail[tier] = tier_fail.get(tier, 0) + 1
            failing.append(entry)

    all_tiers = set(list(tier_pass.keys()) + list(tier_fail.keys()))
    for t in all_tiers:
        report.tier_breakdown[t] = {
            "pass": tier_pass.get(t, 0),
            "fail": tier_fail.get(t, 0),
        }

    # Top failing: smallest awards first (most at risk)
    report.top_failing = sorted(failing, key=lambda x: x["largest_single"])[:20]
    # True count before capping, then cap display list at 20
    report.total_pass_only_90d = len(pass_only_90d_list)
    report.top_pass_only_90d = sorted(
        pass_only_90d_list, key=lambda x: x["recent_total_90d"], reverse=True
    )[:20]

    return report
