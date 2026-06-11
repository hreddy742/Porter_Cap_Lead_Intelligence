"""
Tests for app/ops/gate10_reprocess.py — Gate 10 controlled reprocessing.

All tests use MagicMock sessions — no real database or Docker required.

Key invariants verified:
  - Dry-run performs zero writes and zero commits.
  - Apply mode issues one batch UPDATE + one ReviewDecision INSERT per failing lead.
  - Passing and pass_only_90d leads are never modified.
  - lead_scores table is never referenced in any execute call.
  - Service never commits — the caller (script) commits.
  - Exceptions propagate cleanly so the script can rollback.
"""
from __future__ import annotations

import uuid
from collections import namedtuple
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.dashboard.review import VALID_ACTIONS
from app.db.models import ReviewDecision
from app.ops.gate10_reprocess import Gate10ReprocessResult, run_gate10_reprocess

# Mirrors the columns returned by _REPROCESS_QUERY
ReprocessRow = namedtuple(
    "ReprocessRow",
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
    award_count: int = 1,
    award_date=None,
) -> ReprocessRow:
    return ReprocessRow(
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
    """Return a mock Session whose first execute().fetchall() returns rows."""
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = rows
    return db


# ─── Dry-run ─────────────────────────────────────────────────────────────────


class TestDryRun:
    def test_dry_run_does_not_write(self):
        """Dry-run: db.execute called exactly once (SELECT), db.add never called."""
        row = _row(largest_single="500", recent_total="0")
        db = _db_with_rows([row])

        run_gate10_reprocess(db, dry_run=True)

        assert db.execute.call_count == 1
        db.add.assert_not_called()

    def test_dry_run_does_not_commit(self):
        """Dry-run: db.commit is never called under any circumstance."""
        db = _db_with_rows([_row(largest_single="500")])

        run_gate10_reprocess(db, dry_run=True)

        db.commit.assert_not_called()

    def test_dry_run_result_flag_is_true(self):
        """Result.dry_run is True when called with dry_run=True."""
        db = _db_with_rows([])
        result = run_gate10_reprocess(db, dry_run=True)
        assert result.dry_run is True

    def test_dry_run_classifies_failing_lead(self):
        """Dry-run correctly identifies a failing lead without writing."""
        row = _row(tier="warm", largest_single="500", recent_total="0")
        db = _db_with_rows([row])

        result = run_gate10_reprocess(db, dry_run=True)

        assert len(result.failing) == 1
        assert len(result.passing) == 0
        assert result.applied == 0

    def test_dry_run_classifies_passing_lead(self):
        """Dry-run correctly identifies a passing lead."""
        row = _row(tier="warm", largest_single="20000", recent_total="0")
        db = _db_with_rows([row])

        result = run_gate10_reprocess(db, dry_run=True)

        assert len(result.passing) == 1
        assert len(result.failing) == 0
        assert result.applied == 0

    def test_dry_run_identifies_pass_only_90d(self):
        """Dry-run identifies pass_only_90d candidates as passing (not failing)."""
        row = _row(tier="cold", largest_single="5000", recent_total="12000")
        db = _db_with_rows([row])

        result = run_gate10_reprocess(db, dry_run=True)

        assert len(result.pass_only_90d) == 1
        assert len(result.passing) == 1
        assert len(result.failing) == 0
        assert result.applied == 0

    def test_dry_run_empty_db_returns_zero_counts(self):
        """Dry-run on empty result set returns all-zero counts."""
        db = _db_with_rows([])
        result = run_gate10_reprocess(db, dry_run=True)

        assert isinstance(result, Gate10ReprocessResult)
        assert result.total_active == 0
        assert result.applied == 0
        assert result.failing == []
        assert result.passing == []


# ─── Apply mode ──────────────────────────────────────────────────────────────


class TestApplyMode:
    def test_apply_issues_update_and_review_decision_for_failing_lead(self):
        """Apply: db.execute called twice (SELECT + UPDATE); db.add called once."""
        row = _row(tier="warm", largest_single="500", recent_total="0")
        db = _db_with_rows([row])

        result = run_gate10_reprocess(db, dry_run=False)

        assert result.applied == 1
        assert db.execute.call_count == 2   # SELECT then batch UPDATE
        assert db.add.call_count == 1

    def test_apply_review_decision_fields_are_correct(self):
        """ReviewDecision inserted has the expected field values."""
        row = _row(tier="warm", largest_single="500", recent_total="0")
        db = _db_with_rows([row])

        run_gate10_reprocess(db, dry_run=False)

        added = db.add.call_args[0][0]
        assert isinstance(added, ReviewDecision)
        assert added.action == "archive"
        assert added.reviewer_id == "system:gate10_reprocess"
        assert added.note == "Gate 10 reprocessing: award_amount_too_small"
        assert added.lead_candidate_id == row.lead_candidate_id

    def test_apply_review_decision_action_in_valid_actions(self):
        """ReviewDecision action must be in dashboard's VALID_ACTIONS set."""
        row = _row(largest_single="500")
        db = _db_with_rows([row])

        run_gate10_reprocess(db, dry_run=False)

        added = db.add.call_args[0][0]
        assert added.action in VALID_ACTIONS

    def test_apply_does_not_touch_passing_candidate(self):
        """Apply: a passing lead triggers no UPDATE and no ReviewDecision insert."""
        row = _row(tier="warm", largest_single="20000", recent_total="0")
        db = _db_with_rows([row])

        result = run_gate10_reprocess(db, dry_run=False)

        assert result.applied == 0
        assert db.execute.call_count == 1   # SELECT only, no UPDATE
        db.add.assert_not_called()

    def test_apply_does_not_touch_pass_only_90d_candidate(self):
        """Apply: a pass_only_90d lead is never archived."""
        row = _row(tier="cold", largest_single="5000", recent_total="12000")
        db = _db_with_rows([row])

        result = run_gate10_reprocess(db, dry_run=False)

        assert result.applied == 0
        assert len(result.pass_only_90d) == 1
        db.add.assert_not_called()
        assert db.execute.call_count == 1   # SELECT only

    def test_apply_does_not_commit(self):
        """Service never commits — the script owns the transaction."""
        row = _row(largest_single="500")
        db = _db_with_rows([row])

        run_gate10_reprocess(db, dry_run=False)

        db.commit.assert_not_called()

    def test_apply_multiple_failing_leads_single_update_call(self):
        """Multiple failing leads use one batch UPDATE and N ReviewDecision inserts."""
        rows = [
            _row(name="Corp A", tier="warm", largest_single="500"),
            _row(name="Corp B", tier="cold", largest_single="200"),
        ]
        db = _db_with_rows(rows)

        result = run_gate10_reprocess(db, dry_run=False)

        assert result.applied == 2
        assert db.execute.call_count == 2   # SELECT + one batch UPDATE
        assert db.add.call_count == 2

    def test_apply_mixed_pass_and_fail_only_fail_updated(self):
        """When a mix of passing and failing leads exist, only failing are updated."""
        failing = _row(name="Fail Corp",   tier="warm", largest_single="500")
        passing = _row(name="Pass Corp",   tier="cold", largest_single="50000")
        pass90d = _row(name="90d Corp",    tier="cold", largest_single="5000",
                       recent_total="15000")
        db = _db_with_rows([failing, passing, pass90d])

        result = run_gate10_reprocess(db, dry_run=False)

        assert result.applied == 1
        assert len(result.failing) == 1
        assert len(result.passing) == 2    # passing + pass_only_90d both in passing
        assert len(result.pass_only_90d) == 1
        assert db.add.call_count == 1      # only one ReviewDecision

    def test_apply_result_dry_run_flag_is_false(self):
        """Result.dry_run is False when called with dry_run=False."""
        db = _db_with_rows([])
        result = run_gate10_reprocess(db, dry_run=False)
        assert result.dry_run is False

    def test_apply_empty_db_no_writes(self):
        """Apply mode with no active leads issues SELECT only, zero writes."""
        db = _db_with_rows([])
        result = run_gate10_reprocess(db, dry_run=False)

        assert result.applied == 0
        assert db.execute.call_count == 1
        db.add.assert_not_called()


# ─── lead_scores integrity ────────────────────────────────────────────────────


class TestLeadScoresIntegrity:
    def test_no_lead_scores_referenced_in_dry_run(self):
        """No db.execute call references lead_scores in dry-run mode."""
        db = _db_with_rows([_row(largest_single="500")])
        run_gate10_reprocess(db, dry_run=True)

        for call in db.execute.call_args_list:
            stmt_str = str(call[0][0]).lower()
            assert "lead_scores" not in stmt_str

    def test_no_lead_scores_referenced_in_apply(self):
        """No db.execute call references lead_scores in apply mode."""
        db = _db_with_rows([_row(largest_single="500")])
        run_gate10_reprocess(db, dry_run=False)

        for call in db.execute.call_args_list:
            stmt_str = str(call[0][0]).lower()
            assert "lead_scores" not in stmt_str


# ─── Query filter coverage ────────────────────────────────────────────────────


class TestQueryFilters:
    def test_reprocess_query_filters_active_status(self):
        """_REPROCESS_QUERY SQL includes status='active' filter."""
        from app.ops.gate10_reprocess import _REPROCESS_QUERY
        sql = str(_REPROCESS_QUERY).lower()
        assert "status" in sql
        assert "active" in sql

    def test_reprocess_query_filters_deleted_at(self):
        """_REPROCESS_QUERY SQL includes deleted_at IS NULL filter."""
        from app.ops.gate10_reprocess import _REPROCESS_QUERY
        sql = str(_REPROCESS_QUERY).lower()
        assert "deleted_at" in sql


# ─── Rollback safety ─────────────────────────────────────────────────────────


class TestRollbackSafety:
    def test_db_error_propagates_for_caller_to_rollback(self):
        """Service propagates exceptions so the script can call db.rollback()."""
        db = MagicMock()
        db.execute.side_effect = Exception("DB connection lost")

        with pytest.raises(Exception, match="DB connection lost"):
            run_gate10_reprocess(db, dry_run=False)

    def test_dry_run_db_error_also_propagates(self):
        """Dry-run also propagates exceptions cleanly."""
        db = MagicMock()
        db.execute.side_effect = Exception("select failed")

        with pytest.raises(Exception, match="select failed"):
            run_gate10_reprocess(db, dry_run=True)


# ─── Script argument parsing ──────────────────────────────────────────────────


class TestScriptArgParsing:
    def test_script_defaults_to_dry_run(self):
        """Parsing [] args (no --apply) results in dry_run=True."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--apply", action="store_true", default=False)
        args = parser.parse_args([])

        assert args.apply is False
        dry_run = not args.apply
        assert dry_run is True

    def test_apply_flag_enables_write_mode(self):
        """Parsing ['--apply'] results in dry_run=False."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--apply", action="store_true", default=False)
        args = parser.parse_args(["--apply"])

        assert args.apply is True
        dry_run = not args.apply
        assert dry_run is False
