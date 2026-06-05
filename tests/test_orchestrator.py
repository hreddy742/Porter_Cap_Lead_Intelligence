"""
Tests for app/pipeline/orchestrator.py — Task 08.

All DB interactions use MagicMock(). Connector and processing functions are
patched at the orchestrator module level. No real HTTP calls are made.

Test coverage:
  1.  No enabled sources → completed, sources_total=0
  2.  One USASpending source → connector is called
  3.  Raw event flows extract → resolve → signals → score in correct order
  4.  Empty evidence list → pipeline does not crash
  5.  Company resolution returns None → signal/scoring are skipped
  6.  One source fails, one succeeds → partial
  7.  All sources fail → failed
  8.  source_run.status = completed on success
  9.  source_run.status = failed on exception
  10. pipeline_run.status updated at the end
  11. Summary counts correct for a simple 2-event run
  12. No real HTTP/API call is made (connector is always mocked)
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.db.models import (
    Company,
    EvidenceItem,
    PipelineRun,
    RawSourceEvent,
    Signal,
    SourceRegistry,
    SourceRun,
)
from app.pipeline.orchestrator import run_pipeline

# ─── Patch-target constants ────────────────────────────────────────────────────

_MOD = "app.pipeline.orchestrator"
_LOAD_SOURCES = f"{_MOD}._load_enabled_sources"
_GET_EVENTS = f"{_MOD}._get_raw_events"
_CONNECTOR = f"{_MOD}.USASpendingConnector"
_EXTRACT = f"{_MOD}.extract_evidence"
_RESOLVE = f"{_MOD}.resolve_company_for_evidence"
_SIGNALS = f"{_MOD}.detect_signals_for_evidence"
_SCORE = f"{_MOD}.score_company"


# ─── Object builders ──────────────────────────────────────────────────────────


def _make_db() -> MagicMock:
    return MagicMock()


def _make_source(name: str = "usaspending") -> MagicMock:
    src = MagicMock(spec=SourceRegistry)
    src.id = uuid.uuid4()
    src.name = name
    src.error_text = None
    src.status = "enabled"
    return src


def _make_raw_event() -> MagicMock:
    ev = MagicMock(spec=RawSourceEvent)
    ev.id = uuid.uuid4()
    return ev


def _make_evidence() -> MagicMock:
    ev = MagicMock(spec=EvidenceItem)
    ev.id = uuid.uuid4()
    return ev


def _make_company() -> MagicMock:
    c = MagicMock(spec=Company)
    c.id = uuid.uuid4()
    return c


def _make_signal() -> MagicMock:
    s = MagicMock(spec=Signal)
    s.id = uuid.uuid4()
    return s


def _find_added_obj(db: MagicMock, cls):
    """Return the first object of `cls` passed to db.add()."""
    for c in db.add.call_args_list:
        obj = c.args[0]
        if isinstance(obj, cls):
            return obj
    return None


# ─── Test 1: no enabled sources ───────────────────────────────────────────────


def test_no_enabled_sources_returns_completed():
    with patch(_LOAD_SOURCES, return_value=[]):
        result = run_pipeline(_make_db())

    assert result["status"] == "completed"
    assert result["sources_total"] == 0
    assert result["sources_succeeded"] == 0
    assert result["sources_failed"] == 0
    assert result["raw_events_processed"] == 0
    assert result["errors"] == []


# ─── Test 2: one enabled USASpending source calls the connector ───────────────


def test_usaspending_source_calls_connector():
    source = _make_source("usaspending")
    mock_instance = MagicMock()

    with patch(_LOAD_SOURCES, return_value=[source]), \
         patch(_GET_EVENTS, return_value=[]), \
         patch(_CONNECTOR, return_value=mock_instance):
        run_pipeline(_make_db())

    mock_instance.run.assert_called_once()


# ─── Test 3: raw event flows through the full pipeline in the correct order ───


def test_full_pipeline_flow_correct_order():
    source = _make_source("usaspending")
    raw_event = _make_raw_event()
    evidence = _make_evidence()
    company = _make_company()

    call_order: list[str] = []

    def fake_extract(eid, db):
        call_order.append(f"extract:{eid}")
        return [evidence]

    def fake_resolve(eid, db):
        call_order.append(f"resolve:{eid}")
        return company

    def fake_signals(eid, db):
        call_order.append(f"signals:{eid}")
        return [_make_signal()]

    def fake_score(cid, db):
        call_order.append(f"score:{cid}")
        return {"scored": True}

    with patch(_LOAD_SOURCES, return_value=[source]), \
         patch(_GET_EVENTS, return_value=[raw_event]), \
         patch(_CONNECTOR, return_value=MagicMock()), \
         patch(_EXTRACT, side_effect=fake_extract), \
         patch(_RESOLVE, side_effect=fake_resolve), \
         patch(_SIGNALS, side_effect=fake_signals), \
         patch(_SCORE, side_effect=fake_score):
        result = run_pipeline(_make_db())

    assert call_order == [
        f"extract:{raw_event.id}",
        f"resolve:{evidence.id}",
        f"signals:{evidence.id}",
        f"score:{company.id}",
    ]
    assert result["raw_events_processed"] == 1
    assert result["evidence_items_created"] == 1
    assert result["companies_resolved"] == 1
    assert result["signals_created"] == 1
    assert result["companies_scored"] == 1


# ─── Test 4: empty evidence list does not crash ───────────────────────────────


def test_empty_evidence_list_does_not_crash():
    source = _make_source("usaspending")
    raw_event = _make_raw_event()

    with patch(_LOAD_SOURCES, return_value=[source]), \
         patch(_GET_EVENTS, return_value=[raw_event]), \
         patch(_CONNECTOR, return_value=MagicMock()), \
         patch(_EXTRACT, return_value=[]), \
         patch(_RESOLVE) as mock_resolve, \
         patch(_SIGNALS) as mock_signals, \
         patch(_SCORE) as mock_score:
        result = run_pipeline(_make_db())

    assert result["status"] == "completed"
    assert result["raw_events_processed"] == 1
    assert result["evidence_items_created"] == 0
    mock_resolve.assert_not_called()
    mock_signals.assert_not_called()
    mock_score.assert_not_called()


# ─── Test 5: resolution returns None → signal and scoring are skipped ─────────


def test_resolution_none_skips_signals_and_scoring():
    source = _make_source("usaspending")
    raw_event = _make_raw_event()
    evidence = _make_evidence()

    with patch(_LOAD_SOURCES, return_value=[source]), \
         patch(_GET_EVENTS, return_value=[raw_event]), \
         patch(_CONNECTOR, return_value=MagicMock()), \
         patch(_EXTRACT, return_value=[evidence]), \
         patch(_RESOLVE, return_value=None), \
         patch(_SIGNALS) as mock_signals, \
         patch(_SCORE) as mock_score:
        result = run_pipeline(_make_db())

    assert result["status"] == "completed"
    assert result["evidence_items_created"] == 1
    assert result["companies_resolved"] == 0
    mock_signals.assert_not_called()
    mock_score.assert_not_called()


# ─── Test 6: one source fails, one succeeds → partial ─────────────────────────


def test_partial_when_one_source_fails_one_succeeds():
    source1 = _make_source("usaspending")
    source2 = _make_source("usaspending")

    call_count = {"n": 0}

    def get_events_side_effect(source_run_id, db):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return []  # source1 succeeds with zero events
        raise RuntimeError("source2 data fetch error")

    with patch(_LOAD_SOURCES, return_value=[source1, source2]), \
         patch(_GET_EVENTS, side_effect=get_events_side_effect), \
         patch(_CONNECTOR, return_value=MagicMock()):
        result = run_pipeline(_make_db())

    assert result["status"] == "partial"
    assert result["sources_total"] == 2
    assert result["sources_succeeded"] == 1
    assert result["sources_failed"] == 1
    assert len(result["errors"]) == 1


# ─── Test 7: all sources fail → failed ────────────────────────────────────────


def test_all_sources_fail_returns_failed():
    source1 = _make_source("usaspending")
    source2 = _make_source("usaspending")

    with patch(_LOAD_SOURCES, return_value=[source1, source2]), \
         patch(_GET_EVENTS, side_effect=RuntimeError("data error")), \
         patch(_CONNECTOR, return_value=MagicMock()):
        result = run_pipeline(_make_db())

    assert result["status"] == "failed"
    assert result["sources_succeeded"] == 0
    assert result["sources_failed"] == 2
    assert len(result["errors"]) == 2


# ─── Test 8: source_run.status = completed on success ─────────────────────────


def test_source_run_status_completed_on_success():
    source = _make_source("usaspending")
    db = _make_db()

    with patch(_LOAD_SOURCES, return_value=[source]), \
         patch(_GET_EVENTS, return_value=[]), \
         patch(_CONNECTOR, return_value=MagicMock()):
        run_pipeline(db)

    source_run_obj = _find_added_obj(db, SourceRun)
    assert source_run_obj is not None
    assert source_run_obj.status == "completed"


# ─── Test 9: source_run.status = failed on exception ─────────────────────────


def test_source_run_status_failed_on_exception():
    source = _make_source("usaspending")
    db = _make_db()

    with patch(_LOAD_SOURCES, return_value=[source]), \
         patch(_GET_EVENTS, side_effect=RuntimeError("boom")), \
         patch(_CONNECTOR, return_value=MagicMock()):
        run_pipeline(db)

    source_run_obj = _find_added_obj(db, SourceRun)
    assert source_run_obj is not None
    assert source_run_obj.status == "failed"
    assert "boom" in (source_run_obj.error_text or "")


# ─── Test 10: pipeline_run.status updated at the end ─────────────────────────


def test_pipeline_run_status_updated_at_end():
    db = _make_db()

    with patch(_LOAD_SOURCES, return_value=[]):
        run_pipeline(db)

    pipeline_run_obj = _find_added_obj(db, PipelineRun)
    assert pipeline_run_obj is not None
    assert pipeline_run_obj.status == "completed"
    assert pipeline_run_obj.ended_at is not None


# ─── Test 11: summary counts correct for a simple 2-event successful run ─────


def test_summary_counts_correct_for_two_events():
    source = _make_source("usaspending")
    raw_event1 = _make_raw_event()
    raw_event2 = _make_raw_event()
    evidence1 = _make_evidence()
    evidence2 = _make_evidence()
    company1 = _make_company()
    company2 = _make_company()

    evidence_map = {raw_event1.id: [evidence1], raw_event2.id: [evidence2]}
    company_map = {evidence1.id: company1, evidence2.id: company2}

    with patch(_LOAD_SOURCES, return_value=[source]), \
         patch(_GET_EVENTS, return_value=[raw_event1, raw_event2]), \
         patch(_CONNECTOR, return_value=MagicMock()), \
         patch(_EXTRACT, side_effect=lambda eid, db: evidence_map.get(eid, [])), \
         patch(_RESOLVE, side_effect=lambda eid, db: company_map.get(eid)), \
         patch(_SIGNALS, side_effect=lambda eid, db: [_make_signal()]), \
         patch(_SCORE, return_value={"scored": True}):
        result = run_pipeline(_make_db())

    assert result["status"] == "completed"
    assert result["sources_total"] == 1
    assert result["sources_succeeded"] == 1
    assert result["sources_failed"] == 0
    assert result["raw_events_processed"] == 2
    assert result["evidence_items_created"] == 2
    assert result["companies_resolved"] == 2
    assert result["signals_created"] == 2
    assert result["companies_scored"] == 2


# ─── Test 12: no real HTTP/API call is made ───────────────────────────────────


def test_no_real_http_call_is_made():
    """
    USASpendingConnector is always replaced by a mock. If the real class ran,
    it would attempt an HTTP POST to api.usaspending.gov and fail (no server).
    Passing this test confirms the mock is in place.
    """
    source = _make_source("usaspending")

    with patch(_LOAD_SOURCES, return_value=[source]), \
         patch(_GET_EVENTS, return_value=[]), \
         patch(_CONNECTOR) as mock_cls:
        run_pipeline(_make_db())

    # Constructor was called with (db, source_run, source)
    mock_cls.assert_called_once()
    # run() was called on the instance
    mock_cls.return_value.run.assert_called_once()
