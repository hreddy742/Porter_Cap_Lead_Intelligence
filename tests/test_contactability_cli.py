"""
Tests for scripts/contactability_lite.py

No real DB, no network calls. Uses MagicMock and monkeypatching.

Tests:
  1.  missing --limit without --company-id exits non-zero
  2.  --dry-run without --apply forces dry-run mode
  3.  no --apply flag forces dry-run mode
  4.  --apply enables apply mode
  5.  dry-run result prints correct message
  6.  --company-id is passed through to orchestrator
  7.  --tier all maps to tier=None
  8.  --tier warm passed through
  9.  --source sam passed through
  10. --limit is passed through to orchestrator
"""
from __future__ import annotations

import sys
import os
import uuid
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import the module under test
import scripts.contactability_lite as cli_module


_DRY_RUN_RESULT = {
    "dry_run": True,
    "companies_would_enrich": 5,
    "tier": "warm",
    "source": "all",
    "limit": 25,
}

_APPLY_RESULT = {
    "dry_run": False,
    "run_id": str(uuid.uuid4()),
    "companies_attempted": 5,
    "companies_enriched": 5,
    "companies_failed": 0,
    "tier": "warm",
    "source": "all",
}


def _mock_session(result: dict):
    """Return a mocked SessionLocal context manager that patches run_contactability_enrichment."""
    mock_db = MagicMock()
    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_db)
    mock_ctx.__exit__ = MagicMock(return_value=False)
    return mock_ctx, result


# ── Test 1: missing --limit without --company-id exits non-zero ───────────────

def test_missing_limit_exits_nonzero():
    with patch.object(sys, "argv", ["contactability_lite.py"]):
        with pytest.raises(SystemExit) as exc_info:
            cli_module.main()
    assert exc_info.value.code != 0


# ── Test 2: --dry-run without --apply forces dry-run ─────────────────────────

def test_dry_run_flag_calls_orchestrator_with_dry_run_true(capsys):
    mock_ctx, result = _mock_session(_DRY_RUN_RESULT)
    with patch("scripts.contactability_lite.SessionLocal", return_value=mock_ctx), \
         patch("scripts.contactability_lite.run_contactability_enrichment", return_value=result) as mock_enrich:
        with patch.object(sys, "argv", ["contactability_lite.py", "--limit", "25", "--dry-run"]):
            cli_module.main()

    call_kwargs = mock_enrich.call_args.kwargs
    assert call_kwargs["dry_run"] is True
    assert call_kwargs["apply"] is False


# ── Test 3: no --apply forces dry-run ────────────────────────────────────────

def test_no_apply_forces_dry_run(capsys):
    mock_ctx, result = _mock_session(_DRY_RUN_RESULT)
    with patch("scripts.contactability_lite.SessionLocal", return_value=mock_ctx), \
         patch("scripts.contactability_lite.run_contactability_enrichment", return_value=result) as mock_enrich:
        with patch.object(sys, "argv", ["contactability_lite.py", "--limit", "25"]):
            cli_module.main()

    call_kwargs = mock_enrich.call_args.kwargs
    assert call_kwargs["dry_run"] is True
    assert call_kwargs["apply"] is False


# ── Test 4: --apply enables apply mode ───────────────────────────────────────

def test_apply_flag_sets_apply_true(capsys):
    mock_ctx, result = _mock_session(_APPLY_RESULT)
    with patch("scripts.contactability_lite.SessionLocal", return_value=mock_ctx), \
         patch("scripts.contactability_lite.run_contactability_enrichment", return_value=result) as mock_enrich:
        with patch.object(sys, "argv", ["contactability_lite.py", "--limit", "25", "--apply"]):
            cli_module.main()

    call_kwargs = mock_enrich.call_args.kwargs
    assert call_kwargs["apply"] is True


# ── Test 5: dry-run output contains expected message ─────────────────────────

def test_dry_run_output_message(capsys):
    mock_ctx, result = _mock_session(_DRY_RUN_RESULT)
    with patch("scripts.contactability_lite.SessionLocal", return_value=mock_ctx), \
         patch("scripts.contactability_lite.run_contactability_enrichment", return_value=result):
        with patch.object(sys, "argv", ["contactability_lite.py", "--limit", "25", "--dry-run"]):
            cli_module.main()

    out = capsys.readouterr().out
    assert "dry run" in out.lower()
    assert "no writes" in out.lower()
    assert "no http calls" in out.lower()


# ── Test 6: --company-id is passed through ───────────────────────────────────

def test_company_id_passed_to_orchestrator():
    target_id = str(uuid.uuid4())
    mock_ctx, result = _mock_session({**_DRY_RUN_RESULT, "companies_would_enrich": 1})
    with patch("scripts.contactability_lite.SessionLocal", return_value=mock_ctx), \
         patch("scripts.contactability_lite.run_contactability_enrichment", return_value=result) as mock_enrich:
        with patch.object(sys, "argv", [
            "contactability_lite.py", "--company-id", target_id, "--dry-run"
        ]):
            cli_module.main()

    call_kwargs = mock_enrich.call_args.kwargs
    assert call_kwargs["company_id"] == target_id


# ── Test 7: --tier all maps to tier=None ─────────────────────────────────────

def test_tier_all_passes_none_to_orchestrator():
    mock_ctx, result = _mock_session(_DRY_RUN_RESULT)
    with patch("scripts.contactability_lite.SessionLocal", return_value=mock_ctx), \
         patch("scripts.contactability_lite.run_contactability_enrichment", return_value=result) as mock_enrich:
        with patch.object(sys, "argv", [
            "contactability_lite.py", "--limit", "25", "--tier", "all", "--dry-run"
        ]):
            cli_module.main()

    call_kwargs = mock_enrich.call_args.kwargs
    assert call_kwargs["tier"] is None


# ── Test 8: --tier warm passed through ───────────────────────────────────────

def test_tier_warm_passed_to_orchestrator():
    mock_ctx, result = _mock_session(_DRY_RUN_RESULT)
    with patch("scripts.contactability_lite.SessionLocal", return_value=mock_ctx), \
         patch("scripts.contactability_lite.run_contactability_enrichment", return_value=result) as mock_enrich:
        with patch.object(sys, "argv", [
            "contactability_lite.py", "--limit", "25", "--tier", "warm", "--dry-run"
        ]):
            cli_module.main()

    call_kwargs = mock_enrich.call_args.kwargs
    assert call_kwargs["tier"] == "warm"


# ── Test 9: --source sam passed through ──────────────────────────────────────

def test_source_sam_passed_to_orchestrator():
    mock_ctx, result = _mock_session(_DRY_RUN_RESULT)
    with patch("scripts.contactability_lite.SessionLocal", return_value=mock_ctx), \
         patch("scripts.contactability_lite.run_contactability_enrichment", return_value=result) as mock_enrich:
        with patch.object(sys, "argv", [
            "contactability_lite.py", "--limit", "25", "--source", "sam", "--dry-run"
        ]):
            cli_module.main()

    call_kwargs = mock_enrich.call_args.kwargs
    assert call_kwargs["source"] == "sam"


# ── Test 10: --limit is passed through ───────────────────────────────────────

def test_limit_passed_to_orchestrator():
    mock_ctx, result = _mock_session(_DRY_RUN_RESULT)
    with patch("scripts.contactability_lite.SessionLocal", return_value=mock_ctx), \
         patch("scripts.contactability_lite.run_contactability_enrichment", return_value=result) as mock_enrich:
        with patch.object(sys, "argv", [
            "contactability_lite.py", "--limit", "42", "--dry-run"
        ]):
            cli_module.main()

    call_kwargs = mock_enrich.call_args.kwargs
    assert call_kwargs["limit"] == 42
