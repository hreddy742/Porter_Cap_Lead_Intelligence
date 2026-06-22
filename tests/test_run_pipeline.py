"""
Tests for run_once() / run_loop() scheduling behavior in scripts/run_pipeline.py.

Coverage:
  1. A pipeline error does not crash the loop — it logs and retries.
  2. The loop calls time.sleep with interval_hours * 3600 seconds.
  3. KeyboardInterrupt exits cleanly and logs pipeline_shutdown_requested.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
import structlog.testing

import scripts.run_pipeline as _mod


# ─── Test 1: error in one cycle does not crash the loop ───────────────────────

def test_loop_continues_after_pipeline_error():
    """RuntimeError from run_once must be caught; the loop continues and retries."""
    # Call 1: fail, Call 2: succeed, Call 3: stop via KeyboardInterrupt
    side_effects = [RuntimeError("transient error"), None, KeyboardInterrupt()]

    with (
        patch("scripts.run_pipeline.run_once", side_effect=side_effects),
        patch("time.sleep") as mock_sleep,
        structlog.testing.capture_logs() as cap,
        pytest.raises(KeyboardInterrupt),
    ):
        _mod.run_loop(interval_hours=1)

    # Sleep was called after the failed run and after the successful run
    assert mock_sleep.call_count == 2
    mock_sleep.assert_called_with(1 * 3600)

    # The failure was logged
    events = [e["event"] for e in cap]
    assert "pipeline_cycle_failed" in events


# ─── Test 2: interval is respected ────────────────────────────────────────────

def test_loop_respects_interval_hours():
    """time.sleep must receive exactly interval_hours * 3600 seconds."""
    side_effects = [None, KeyboardInterrupt()]

    with (
        patch("scripts.run_pipeline.run_once", side_effect=side_effects),
        patch("time.sleep") as mock_sleep,
        pytest.raises(KeyboardInterrupt),
    ):
        _mod.run_loop(interval_hours=3)

    mock_sleep.assert_called_once_with(3 * 3600)


# ─── Test 3: KeyboardInterrupt exits cleanly ──────────────────────────────────

def test_keyboard_interrupt_exits_cleanly():
    """KeyboardInterrupt must propagate as KeyboardInterrupt (not SystemExit or Exception)."""
    with (
        patch("scripts.run_pipeline.run_once", side_effect=KeyboardInterrupt()),
        patch("time.sleep"),
        structlog.testing.capture_logs() as cap,
        pytest.raises(KeyboardInterrupt),
    ):
        _mod.run_loop(interval_hours=1)

    events = [e["event"] for e in cap]
    assert "pipeline_shutdown_requested" in events
