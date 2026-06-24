"""
Tests for app/ops/quality_comparison.py.

Strategy:
  - Before constants: verify hardcoded values and arithmetic identity.
  - DB query functions: MagicMock sessions.
  - build_after_snapshot: patch build_gate10_impact_report + DB helpers.
  - build_comparison: patch build_after_snapshot.
  - compute_changes: pure-function tests, no DB needed.
  - INTERPRETATION_NOTES: verify caution language is present.

No real database required. All tests run with pytest -q without Docker.
"""
from __future__ import annotations

from collections import namedtuple
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from app.ops.gate_impact import Gate10ImpactReport
from app.ops.quality_comparison import (
    BEFORE_ACTIVE_LEAD_CANDIDATES,
    BEFORE_ARCHIVED_BY_GATE10,
    BEFORE_COLD,
    BEFORE_GATE10_FAILING,
    BEFORE_GATE10_PASSING_SINGLE,
    BEFORE_PASS_ONLY_90D,
    BEFORE_WARM,
    INTERPRETATION_NOTES,
    AfterSnapshot,
    BeforeState,
    ComparisonReport,
    build_after_snapshot,
    build_comparison,
    compute_changes,
    count_gate10_archived,
    get_gate10_archived_names,
)


# ── Before-state constants ─────────────────────────────────────────────────────

class TestBeforeStateConstants:
    def test_active_lead_candidates_is_39(self):
        assert BEFORE_ACTIVE_LEAD_CANDIDATES == 39

    def test_warm_is_35(self):
        assert BEFORE_WARM == 35

    def test_cold_is_4(self):
        assert BEFORE_COLD == 4

    def test_gate10_failing_is_8(self):
        assert BEFORE_GATE10_FAILING == 8

    def test_pass_only_90d_is_1(self):
        assert BEFORE_PASS_ONLY_90D == 1

    def test_archived_by_gate10_is_0(self):
        # Before reprocessing ran, nothing had been archived.
        assert BEFORE_ARCHIVED_BY_GATE10 == 0

    def test_gate10_passing_single_is_30(self):
        assert BEFORE_GATE10_PASSING_SINGLE == 30

    def test_arithmetic_identity(self):
        # passing_single + failing + pass_only_90d must equal total
        assert (
            BEFORE_GATE10_PASSING_SINGLE
            + BEFORE_GATE10_FAILING
            + BEFORE_PASS_ONLY_90D
            == BEFORE_ACTIVE_LEAD_CANDIDATES
        )

    def test_before_state_dataclass_defaults_match_constants(self):
        b = BeforeState()
        assert b.active_lead_candidates == BEFORE_ACTIVE_LEAD_CANDIDATES
        assert b.warm == BEFORE_WARM
        assert b.cold == BEFORE_COLD
        assert b.gate10_failing == BEFORE_GATE10_FAILING
        assert b.pass_only_90d == BEFORE_PASS_ONLY_90D
        assert b.gate10_passing_single == BEFORE_GATE10_PASSING_SINGLE
        assert b.archived_by_gate10 == BEFORE_ARCHIVED_BY_GATE10


# ── count_gate10_archived ──────────────────────────────────────────────────────

class TestCountGate10Archived:
    def _scalar_db(self, value):
        db = MagicMock()
        db.execute.return_value.scalar.return_value = value
        return db

    def test_returns_count_from_db(self):
        assert count_gate10_archived(self._scalar_db(8)) == 8

    def test_zero_is_valid(self):
        assert count_gate10_archived(self._scalar_db(0)) == 0

    def test_none_becomes_zero(self):
        # Empty review_decisions table returns NULL from COUNT; handled safely.
        assert count_gate10_archived(self._scalar_db(None)) == 0


# ── get_gate10_archived_names ──────────────────────────────────────────────────

NameRow = namedtuple("NameRow", ["canonical_name"])


class TestGetGate10ArchivedNames:
    def _db_with_rows(self, rows):
        db = MagicMock()
        db.execute.return_value.fetchall.return_value = rows
        return db

    def test_returns_name_list(self):
        rows = [NameRow("Acme Federal"), NameRow("Baltimore Auto Supply")]
        result = get_gate10_archived_names(self._db_with_rows(rows))
        assert result == ["Acme Federal", "Baltimore Auto Supply"]

    def test_empty_review_decisions_returns_empty_list(self):
        result = get_gate10_archived_names(self._db_with_rows([]))
        assert result == []

    def test_single_name(self):
        rows = [NameRow("Some Company")]
        result = get_gate10_archived_names(self._db_with_rows(rows))
        assert result == ["Some Company"]


# ── build_after_snapshot ───────────────────────────────────────────────────────

def _make_impact_report(
    total: int = 31,
    tier_counts: dict | None = None,
    total_pass: int = 31,
    total_fail: int = 0,
    total_pass_only_90d: int = 1,
) -> Gate10ImpactReport:
    if tier_counts is None:
        tier_counts = {"warm": 30, "cold": 1}
    r = Gate10ImpactReport(total_lead_candidates=total)
    r.tier_counts_before = tier_counts
    r.total_pass = total_pass
    r.total_fail = total_fail
    r.total_pass_only_90d = total_pass_only_90d
    return r


class TestBuildAfterSnapshot:
    def _call(
        self,
        impact: Gate10ImpactReport,
        archived_count: int = 8,
        archived_names: list | None = None,
    ) -> AfterSnapshot:
        if archived_names is None:
            archived_names = ["Acme Corp"]
        db = MagicMock()
        with (
            patch(
                "app.ops.quality_comparison.build_gate10_impact_report",
                return_value=impact,
            ),
            patch(
                "app.ops.quality_comparison.count_gate10_archived",
                return_value=archived_count,
            ),
            patch(
                "app.ops.quality_comparison.get_gate10_archived_names",
                return_value=archived_names,
            ),
        ):
            return build_after_snapshot(db)

    def test_typical_after_state(self):
        impact = _make_impact_report(
            total=31,
            tier_counts={"warm": 30, "cold": 1},
            total_pass=31,
            total_fail=0,
            total_pass_only_90d=1,
        )
        snap = self._call(impact, archived_count=8, archived_names=["Corp A"])
        assert snap.active_lead_candidates == 31
        assert snap.warm == 30
        assert snap.cold == 1
        assert snap.gate10_failing == 0
        assert snap.pass_only_90d == 1
        assert snap.gate10_passing_single == 30  # 31 total_pass - 1 pass_only_90d
        assert snap.archived_by_gate10 == 8
        assert snap.archived_company_names == ["Corp A"]

    def test_empty_db_zero_state(self):
        # All zeros — empty database must not raise.
        impact = _make_impact_report(
            total=0,
            tier_counts={},
            total_pass=0,
            total_fail=0,
            total_pass_only_90d=0,
        )
        snap = self._call(impact, archived_count=0, archived_names=[])
        assert snap.active_lead_candidates == 0
        assert snap.warm == 0
        assert snap.cold == 0
        assert snap.gate10_failing == 0
        assert snap.pass_only_90d == 0
        assert snap.gate10_passing_single == 0
        assert snap.archived_by_gate10 == 0
        assert snap.archived_company_names == []

    def test_missing_warm_in_tier_counts_defaults_to_zero(self):
        impact = _make_impact_report(
            total=5,
            tier_counts={"cold": 5},
            total_pass=5,
            total_fail=0,
            total_pass_only_90d=0,
        )
        snap = self._call(impact, archived_count=0, archived_names=[])
        assert snap.warm == 0
        assert snap.cold == 5

    def test_passing_single_derived_from_pass_minus_90d(self):
        # total_pass=10, pass_only_90d=3 → gate10_passing_single=7
        impact = _make_impact_report(
            total=13,
            tier_counts={"warm": 13},
            total_pass=10,
            total_fail=3,
            total_pass_only_90d=3,
        )
        snap = self._call(impact, archived_count=0, archived_names=[])
        assert snap.gate10_passing_single == 7

    def test_no_review_decisions_archived_count_zero(self):
        impact = _make_impact_report()
        snap = self._call(impact, archived_count=0, archived_names=[])
        assert snap.archived_by_gate10 == 0
        assert snap.archived_company_names == []


# ── compute_changes ────────────────────────────────────────────────────────────

def _after_snap(**kwargs) -> AfterSnapshot:
    defaults = {
        "active_lead_candidates": 31,
        "warm": 30,
        "cold": 1,
        "gate10_failing": 0,
        "pass_only_90d": 1,
        "gate10_passing_single": 30,
        "archived_by_gate10": 8,
        "archived_company_names": [],
    }
    defaults.update(kwargs)
    return AfterSnapshot(**defaults)


class TestComputeChanges:
    def test_expected_changes_from_known_baseline(self):
        report = ComparisonReport(before=BeforeState(), after=_after_snap())
        changes = compute_changes(report)
        assert changes["active_lead_candidates"] == -8   # 31 - 39
        assert changes["warm"] == -5                     # 30 - 35
        assert changes["cold"] == -3                     # 1 - 4
        assert changes["gate10_failing"] == -8           # 0 - 8
        assert changes["pass_only_90d"] == 0             # 1 - 1
        assert changes["gate10_passing_single"] == 0     # 30 - 30
        assert changes["archived_by_gate10"] == 8        # 8 - 0

    def test_archived_increases_from_zero(self):
        report = ComparisonReport(before=BeforeState(), after=_after_snap(archived_by_gate10=8))
        assert compute_changes(report)["archived_by_gate10"] == 8

    def test_zero_change_when_after_matches_before(self):
        after = _after_snap(
            active_lead_candidates=39,
            warm=35,
            cold=4,
            gate10_failing=8,
            pass_only_90d=1,
            gate10_passing_single=30,
            archived_by_gate10=0,
        )
        report = ComparisonReport(before=BeforeState(), after=after)
        changes = compute_changes(report)
        assert all(v == 0 for v in changes.values())

    def test_all_expected_keys_present(self):
        report = ComparisonReport(before=BeforeState(), after=_after_snap())
        changes = compute_changes(report)
        expected = {
            "active_lead_candidates",
            "warm",
            "cold",
            "gate10_failing",
            "pass_only_90d",
            "gate10_passing_single",
            "archived_by_gate10",
        }
        assert set(changes.keys()) == expected

    def test_pure_function_no_side_effects(self):
        # Calling twice returns the same result.
        report = ComparisonReport(before=BeforeState(), after=_after_snap())
        assert compute_changes(report) == compute_changes(report)


# ── build_comparison ───────────────────────────────────────────────────────────

class TestBuildComparison:
    def test_assembles_before_and_after(self):
        mock_after = _after_snap()
        db = MagicMock()
        with patch(
            "app.ops.quality_comparison.build_after_snapshot",
            return_value=mock_after,
        ):
            report = build_comparison(db)

        assert isinstance(report.before, BeforeState)
        assert report.after is mock_after

    def test_before_values_are_hardcoded_constants(self):
        db = MagicMock()
        with patch(
            "app.ops.quality_comparison.build_after_snapshot",
            return_value=_after_snap(),
        ):
            report = build_comparison(db)

        # Before-state comes from constants, not the DB.
        assert report.before.active_lead_candidates == 39
        assert report.before.gate10_failing == 8


# ── Interpretation caution language ───────────────────────────────────────────

class TestInterpretationCautionLanguage:
    def _combined(self) -> str:
        return " ".join(INTERPRETATION_NOTES).lower()

    def test_includes_does_not_prove_roi(self):
        assert "does not prove roi" in self._combined()

    def test_includes_does_not_prove_sales_readiness(self):
        assert "does not prove sales readiness" in self._combined()

    def test_includes_does_not_prove_factoring_need(self):
        assert "does not prove factoring need" in self._combined()

    def test_notes_list_is_nonempty(self):
        assert len(INTERPRETATION_NOTES) > 0

    def test_includes_phase2a_improvement_claim(self):
        combined = self._combined()
        assert "phase 2a" in combined
        assert "gate 10" in combined

    def test_includes_honest_active_lead_count_claim(self):
        combined = self._combined()
        assert "39" in combined
        assert "31" in combined
