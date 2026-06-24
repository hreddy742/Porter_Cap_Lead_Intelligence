"""
Tests for app/ops/gate_impact.py.

Strategy:
  - classify_gate10: pure-function tests, no DB needed.
  - build_gate10_impact_report: MagicMock session (consistent with test_lead_quality.py).

No real database is required.  All tests run with `pytest -q` without Docker.
"""
from __future__ import annotations

import uuid
from collections import namedtuple
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.ops.gate_impact import (
    Gate10ImpactReport,
    build_gate10_impact_report,
    classify_gate10,
)

# Mirrors the columns returned by _IMPACT_QUERY in gate_impact.py
ImpactRow = namedtuple(
    "ImpactRow",
    [
        "lead_candidate_id",
        "company_id",
        "tier",
        "canonical_name",
        "largest_single",
        "recent_total_90d",
        "positive_award_count",
        "most_recent_award_date",
    ],
)

_MIN = Decimal("10000")


def _row(
    *,
    tier: str = "warm",
    name: str = "Acme Federal",
    largest_single: str = "0",
    recent_total: str = "0",
    award_count: int = 0,
    award_date=None,
) -> ImpactRow:
    return ImpactRow(
        lead_candidate_id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        tier=tier,
        canonical_name=name,
        largest_single=Decimal(largest_single),
        recent_total_90d=Decimal(recent_total),
        positive_award_count=award_count,
        most_recent_award_date=award_date,
    )


def _db_with_rows(rows: list) -> MagicMock:
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = rows
    return db


# ─── classify_gate10 (pure function) ─────────────────────────────────────────


class TestClassifyGate10:
    def test_single_award_pass(self):
        # largest_single meets threshold → outright pass
        result = classify_gate10(Decimal("15000"), Decimal("0"), _MIN, _MIN)
        assert result == "pass"

    def test_aggregate_only_pass(self):
        # single too small, but 90-day total qualifies
        result = classify_gate10(Decimal("5000"), Decimal("12000"), _MIN, _MIN)
        assert result == "pass_only_90d"

    def test_too_small_fail(self):
        # neither threshold met
        result = classify_gate10(Decimal("5000"), Decimal("5000"), _MIN, _MIN)
        assert result == "fail_too_small"

    def test_zero_amounts_not_qualifying(self):
        # SQL returns 0 for companies whose only signals are zero/negative
        result = classify_gate10(Decimal("0"), Decimal("0"), _MIN, _MIN)
        assert result == "fail_too_small"

    def test_negative_amounts_not_qualifying(self):
        # Negative amounts produce largest_single=0, recent_total=0 after SQL filtering
        result = classify_gate10(Decimal("0"), Decimal("0"), _MIN, _MIN)
        assert result == "fail_too_small"

    def test_old_awards_excluded_from_90d_total(self):
        # A company with only old awards: SQL gives recent_total_90d=0.
        # If the single award (from any time) is also too small, the company fails.
        result = classify_gate10(Decimal("5000"), Decimal("0"), _MIN, _MIN)
        assert result == "fail_too_small"

    def test_old_large_award_still_counts_for_largest_single(self):
        # largest_single has no date restriction — an old large award qualifies.
        result = classify_gate10(Decimal("50000"), Decimal("0"), _MIN, _MIN)
        assert result == "pass"

    def test_exact_threshold_passes(self):
        # exactly 10000 >= 10000 → pass (>= not >)
        result = classify_gate10(Decimal("10000"), Decimal("0"), _MIN, _MIN)
        assert result == "pass"

    def test_exact_90d_threshold_passes(self):
        result = classify_gate10(Decimal("0"), Decimal("10000"), _MIN, _MIN)
        assert result == "pass_only_90d"

    def test_single_win_is_not_labelled_pass_only_90d(self):
        # When single >= threshold, always "pass", even if 90d also qualifies
        result = classify_gate10(Decimal("10000"), Decimal("10000"), _MIN, _MIN)
        assert result == "pass"

    def test_one_cent_below_threshold_fails(self):
        result = classify_gate10(Decimal("9999.99"), Decimal("9999.99"), _MIN, _MIN)
        assert result == "fail_too_small"


# ─── build_gate10_impact_report (mock DB) ─────────────────────────────────────


class TestBuildGate10ImpactReport:
    def test_empty_db_returns_zero_counts(self):
        report = build_gate10_impact_report(_db_with_rows([]))
        assert isinstance(report, Gate10ImpactReport)
        assert report.total_lead_candidates == 0
        assert report.total_pass == 0
        assert report.total_fail == 0
        assert report.tier_counts_before == {}
        assert report.tier_breakdown == {}
        assert report.top_failing == []
        assert report.top_pass_only_90d == []

    def test_all_pass_via_single_award(self):
        rows = [
            _row(tier="warm", largest_single="20000", recent_total="0"),
            _row(tier="cold", largest_single="15000", recent_total="0"),
        ]
        report = build_gate10_impact_report(_db_with_rows(rows))
        assert report.total_lead_candidates == 2
        assert report.total_pass == 2
        assert report.total_fail == 0
        assert report.top_failing == []

    def test_all_fail_too_small(self):
        rows = [
            _row(tier="warm", largest_single="500", recent_total="1000"),
            _row(tier="cold", largest_single="0",   recent_total="0"),
        ]
        report = build_gate10_impact_report(_db_with_rows(rows))
        assert report.total_pass == 0
        assert report.total_fail == 2
        assert len(report.top_failing) == 2

    def test_tier_breakdown_correct(self):
        rows = [
            _row(tier="warm", largest_single="20000"),   # pass
            _row(tier="warm", largest_single="500"),     # fail
            _row(tier="cold", largest_single="500"),     # fail
            _row(tier="cold", largest_single="11000"),   # pass
        ]
        report = build_gate10_impact_report(_db_with_rows(rows))
        assert report.tier_breakdown["warm"]["pass"] == 1
        assert report.tier_breakdown["warm"]["fail"] == 1
        assert report.tier_breakdown["cold"]["pass"] == 1
        assert report.tier_breakdown["cold"]["fail"] == 1

    def test_tier_counts_before_matches_all_candidates(self):
        rows = [
            _row(tier="warm"),
            _row(tier="warm"),
            _row(tier="cold"),
        ]
        report = build_gate10_impact_report(_db_with_rows(rows))
        assert report.tier_counts_before == {"warm": 2, "cold": 1}
        assert report.total_lead_candidates == 3

    def test_pass_only_90d_counted_in_total_pass_and_separate_list(self):
        rows = [
            _row(tier="warm", largest_single="5000", recent_total="12000"),
        ]
        report = build_gate10_impact_report(_db_with_rows(rows))
        assert report.total_pass == 1
        assert report.total_fail == 0
        assert report.total_pass_only_90d == 1
        assert len(report.top_pass_only_90d) == 1
        assert report.top_pass_only_90d[0]["recent_total_90d"] == pytest.approx(12000.0)

    def test_total_pass_only_90d_is_true_count_not_capped(self):
        # 25 pass-only-90d candidates — total must be 25, display list capped at 20
        rows = [
            _row(name=f"Corp {i}", tier="warm", largest_single="0", recent_total="15000")
            for i in range(25)
        ]
        report = build_gate10_impact_report(_db_with_rows(rows))
        assert report.total_pass_only_90d == 25
        assert len(report.top_pass_only_90d) == 20

    def test_pass_only_90d_not_in_top_failing(self):
        rows = [
            _row(tier="cold", largest_single="5000", recent_total="15000"),
        ]
        report = build_gate10_impact_report(_db_with_rows(rows))
        assert report.top_failing == []

    def test_top_failing_sorted_ascending_by_largest_single(self):
        rows = [
            _row(name="B Corp", tier="warm", largest_single="8000"),
            _row(name="A Corp", tier="warm", largest_single="2000"),
            _row(name="C Corp", tier="cold", largest_single="5000"),
        ]
        report = build_gate10_impact_report(_db_with_rows(rows))
        singles = [r["largest_single"] for r in report.top_failing]
        assert singles == sorted(singles)
        assert report.top_failing[0]["canonical_name"] == "A Corp"

    def test_top_pass_only_90d_sorted_descending_by_recent_total(self):
        rows = [
            _row(name="Low 90d",  tier="warm", largest_single="0", recent_total="11000"),
            _row(name="High 90d", tier="warm", largest_single="0", recent_total="50000"),
            _row(name="Mid 90d",  tier="cold", largest_single="0", recent_total="25000"),
        ]
        report = build_gate10_impact_report(_db_with_rows(rows))
        totals = [r["recent_total_90d"] for r in report.top_pass_only_90d]
        assert totals == sorted(totals, reverse=True)
        assert report.top_pass_only_90d[0]["canonical_name"] == "High 90d"

    def test_unscored_tier_treated_as_unscored(self):
        rows = [_row(tier=None, largest_single="500")]
        report = build_gate10_impact_report(_db_with_rows(rows))
        assert "unscored" in report.tier_counts_before
        assert "unscored" in report.tier_breakdown

    def test_thresholds_reflected_in_report(self):
        report = build_gate10_impact_report(_db_with_rows([]))
        assert report.min_single_threshold == Decimal("10000")
        assert report.min_90d_threshold == Decimal("10000")
