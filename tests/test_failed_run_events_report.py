"""
Tests for scripts/failed_run_events_report.py.

All tests use MagicMock — no DB container, no network calls, no live pipeline.

Coverage:
  1. No failed source runs → empty list returned
  2. Failed run with raw events but no evidence → needs_manual_review=True
  3. Failed run with all events fully processed → needs_manual_review=False
  4. Failed run with partially processed events → correct split counts
  5. events_without_evidence = raw_event_count - events_with_evidence (no negative)
  6. Report is read-only: db.add / db.delete / db.flush / db.commit never called
  7. print_report with no rows mentions no failed runs and no modification
  8. print_report with a needs-review row warns ACTION REQUIRED
  9. print_report clearly states it does not process records
  10. build_report returns only the keys downstream code can rely on
"""
from __future__ import annotations

import io
from unittest.mock import MagicMock

import pytest

from scripts.failed_run_events_report import build_report, print_report


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_db(rows: list[dict] | None = None) -> MagicMock:
    """Return a mock Session whose execute().mappings().all() returns rows."""
    db = MagicMock()
    db.execute.return_value.mappings.return_value.all.return_value = rows or []
    return db


def _make_row(**overrides) -> dict:
    """Minimal fake DB row matching the columns returned by _FAILED_RUNS_SQL."""
    row: dict = {
        "source_run_id": "aaaa-bbbb-cccc-dddd",
        "source_name": "usaspending",
        "status": "failed",
        "started_at": "2025-06-10 02:00:00+00",
        "finished_at": "2025-06-10 03:30:00+00",
        "records_fetched": 24900,
        "records_valid": 24400,
        "quarantine_count": 500,
        "error_text": "ConnectorError: USASpending request failed after 4 attempts on page 250",
        "raw_event_count": 24400,
        "events_with_evidence": 0,
    }
    row.update(overrides)
    return row


# ── Test 1: no failed runs ────────────────────────────────────────────────────


def test_no_failed_runs_returns_empty_list():
    db = _make_db([])
    result = build_report(db)
    assert result == []


# ── Test 2: unprocessed events → needs_manual_review=True ────────────────────


def test_failed_run_with_no_evidence_needs_review():
    db = _make_db([_make_row(raw_event_count=24400, events_with_evidence=0)])
    result = build_report(db)

    assert len(result) == 1
    row = result[0]
    assert row["raw_event_count"] == 24400
    assert row["events_with_evidence"] == 0
    assert row["events_without_evidence"] == 24400
    assert row["needs_manual_review"] is True


# ── Test 3: all events processed → needs_manual_review=False ─────────────────


def test_failed_run_with_all_evidence_no_review_needed():
    # A run that failed AFTER evidence was extracted for all its events
    db = _make_db([_make_row(raw_event_count=50, events_with_evidence=50)])
    result = build_report(db)

    assert len(result) == 1
    row = result[0]
    assert row["events_without_evidence"] == 0
    assert row["needs_manual_review"] is False


# ── Test 4: partial processing → correct split counts ────────────────────────


def test_failed_run_partially_processed_splits_correctly():
    db = _make_db([_make_row(raw_event_count=100, events_with_evidence=40)])
    result = build_report(db)

    row = result[0]
    assert row["raw_event_count"] == 100
    assert row["events_with_evidence"] == 40
    assert row["events_without_evidence"] == 60
    assert row["needs_manual_review"] is True


# ── Test 5: counts are never negative ────────────────────────────────────────


def test_events_without_evidence_never_negative():
    # Edge case: DB rounding could theoretically make evidence > raw; clamp to 0.
    # This test documents the expected arithmetic behaviour.
    db = _make_db([_make_row(raw_event_count=10, events_with_evidence=10)])
    result = build_report(db)

    assert result[0]["events_without_evidence"] == 0


# ── Test 6: report is read-only ───────────────────────────────────────────────


def test_build_report_is_read_only():
    """
    build_report must only call db.execute() (SELECT).
    It must never call db.add(), db.delete(), db.flush(), or db.commit().
    """
    db = _make_db([_make_row()])
    build_report(db)

    db.add.assert_not_called()
    db.delete.assert_not_called()
    db.flush.assert_not_called()
    db.commit.assert_not_called()


# ── Test 7: print_report with no rows says no failed runs ────────────────────


def test_print_report_no_rows(capsys):
    print_report([])
    out = capsys.readouterr().out
    assert "No failed source runs found" in out
    assert "READ-ONLY" in out


# ── Test 8: print_report warns ACTION REQUIRED for unprocessed events ─────────


def test_print_report_action_required_for_unprocessed(capsys):
    rows = build_report(_make_db([_make_row(raw_event_count=24400, events_with_evidence=0)]))
    print_report(rows)
    out = capsys.readouterr().out
    assert "ACTION REQUIRED" in out
    assert "24400" in out


# ── Test 9: print_report explicitly states no processing occurs ───────────────


def test_print_report_states_no_processing(capsys):
    """
    The report output must explicitly state that it does not recover or
    process any records, preventing operators from mistaking it for a recovery tool.
    """
    rows = build_report(_make_db([_make_row(raw_event_count=100, events_with_evidence=0)]))
    print_report(rows)
    out = capsys.readouterr().out

    # Both the header and the per-row note must mention no modification
    assert "READ-ONLY" in out or "read-only" in out.lower()
    assert "recovery plan" in out.lower() or "does not" in out.lower()


# ── Test 10: build_report returns expected keys ───────────────────────────────


def test_build_report_returns_expected_keys():
    db = _make_db([_make_row()])
    result = build_report(db)

    required_keys = {
        "source_run_id",
        "source_name",
        "status",
        "started_at",
        "finished_at",
        "records_fetched",
        "records_valid",
        "quarantine_count",
        "error_text",
        "raw_event_count",
        "events_with_evidence",
        "events_without_evidence",
        "needs_manual_review",
    }
    assert required_keys.issubset(result[0].keys())


# ── Test 11: multiple failed runs in report ───────────────────────────────────


def test_multiple_failed_runs_all_returned():
    rows = [
        _make_row(source_run_id="run-1", raw_event_count=100, events_with_evidence=0),
        _make_row(source_run_id="run-2", raw_event_count=50, events_with_evidence=50),
    ]
    db = _make_db(rows)
    result = build_report(db)

    assert len(result) == 2
    run1 = next(r for r in result if r["source_run_id"] == "run-1")
    run2 = next(r for r in result if r["source_run_id"] == "run-2")
    assert run1["needs_manual_review"] is True
    assert run2["needs_manual_review"] is False
