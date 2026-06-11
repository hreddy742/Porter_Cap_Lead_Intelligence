"""
Tests for scripts/run_pipeline.py entrypoint.

All tests use unittest.mock — no real DB, no real HTTP calls.

Coverage:
  1. run_pipeline() is called with the DB session
  2. DB session is closed even when run_pipeline raises
  3. exit code 0 when status == "completed"
  4. exit code 1 when status == "failed"
  5. exit code 1 when status == "partial"
  6. Summary dict is printed to stdout
"""
from __future__ import annotations

import importlib
import json
import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest


def _run_main(summary: dict) -> tuple[int, str]:
    """
    Execute scripts.run_pipeline.main() with run_pipeline mocked to return
    *summary* and SessionLocal mocked.  Returns (exit_code, stdout_text).
    """
    mock_db = MagicMock()
    mock_session_local = MagicMock(return_value=mock_db)

    with (
        patch("scripts.run_pipeline.SessionLocal", mock_session_local),
        patch("scripts.run_pipeline.run_pipeline", return_value=summary) as mock_rp,
        patch("builtins.print") as mock_print,
        pytest.raises(SystemExit) as exc_info,
    ):
        import scripts.run_pipeline as mod
        mod.main()

    stdout_calls = [str(call) for call in mock_print.call_args_list]
    return exc_info.value.code, "\n".join(stdout_calls), mock_rp, mock_db, mock_session_local


# ─── helper that returns all 5 values cleanly ─────────────────────────────────

def _execute(summary: dict):
    mock_db = MagicMock()
    mock_session_local = MagicMock(return_value=mock_db)
    captured_output: list[str] = []

    with (
        patch("scripts.run_pipeline.SessionLocal", mock_session_local),
        patch("scripts.run_pipeline.run_pipeline", return_value=summary) as mock_rp,
        patch("builtins.print", side_effect=lambda *a, **kw: captured_output.append(str(a[0]) if a else "")),
        pytest.raises(SystemExit) as exc_info,
    ):
        import scripts.run_pipeline as mod
        mod.main()

    return {
        "exit_code": exc_info.value.code,
        "stdout": "\n".join(captured_output),
        "mock_rp": mock_rp,
        "mock_db": mock_db,
    }


_COMPLETED_SUMMARY = {
    "pipeline_run_id": "00000000-0000-0000-0000-000000000001",
    "status": "completed",
    "sources_total": 1,
    "sources_succeeded": 1,
    "sources_failed": 0,
    "raw_events_processed": 5,
    "evidence_items_created": 3,
    "companies_resolved": 2,
    "signals_created": 4,
    "companies_scored": 2,
    "errors": [],
}

_FAILED_SUMMARY = {**_COMPLETED_SUMMARY, "status": "failed", "sources_failed": 1, "sources_succeeded": 0}
_PARTIAL_SUMMARY = {**_COMPLETED_SUMMARY, "status": "partial", "sources_failed": 1}


# ─── Test 1: run_pipeline is called with the DB session ───────────────────────

def test_run_pipeline_is_called_with_db():
    result = _execute(_COMPLETED_SUMMARY)
    result["mock_rp"].assert_called_once()
    call_args = result["mock_rp"].call_args
    assert call_args[0][0] is result["mock_db"]


# ─── Test 2: DB session is always closed ──────────────────────────────────────

def test_db_session_closed_on_success():
    result = _execute(_COMPLETED_SUMMARY)
    result["mock_db"].close.assert_called_once()


def test_db_session_closed_when_run_pipeline_raises():
    mock_db = MagicMock()
    mock_session_local = MagicMock(return_value=mock_db)

    with (
        patch("scripts.run_pipeline.SessionLocal", mock_session_local),
        patch("scripts.run_pipeline.run_pipeline", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError, match="boom"),
    ):
        import scripts.run_pipeline as mod
        mod.main()

    mock_db.close.assert_called_once()


# ─── Test 3: exit code 0 when completed ───────────────────────────────────────

def test_exit_zero_on_completed():
    result = _execute(_COMPLETED_SUMMARY)
    assert result["exit_code"] == 0


# ─── Test 4: exit code 1 when failed ──────────────────────────────────────────

def test_exit_nonzero_on_failed():
    result = _execute(_FAILED_SUMMARY)
    assert result["exit_code"] == 1


# ─── Test 5: exit code 1 when partial ─────────────────────────────────────────

def test_exit_nonzero_on_partial():
    result = _execute(_PARTIAL_SUMMARY)
    assert result["exit_code"] == 1


# ─── Test 6: summary dict is printed ─────────────────────────────────────────

def test_summary_printed_to_stdout():
    result = _execute(_COMPLETED_SUMMARY)
    # The captured output should contain JSON with the expected status
    assert "completed" in result["stdout"]
    assert "sources_total" in result["stdout"]
