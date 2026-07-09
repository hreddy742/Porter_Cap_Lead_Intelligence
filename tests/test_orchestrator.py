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
  13. warm scored lead increments total_warm on pipeline_run
  14. cold scored lead increments total_cold on pipeline_run
  15. hot scored lead increments total_hot on pipeline_run
  16. archive scored lead increments total_archive on pipeline_run
  17. gated (scored=False) lead does not increment any tier counter
  18. quarantine_count from source_run is persisted to pipeline_run
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError

from app.db.models import (
    Company,
    EvidenceItem,
    PipelineRun,
    RawSourceEvent,
    Signal,
    SourceRegistry,
    SourceRun,
)
from app.pipeline.orchestrator import (
    BATCH_COMMIT_SIZE,
    MAX_RETRIES,
    _commit_batch,
    run_pipeline,
)

# ─── Patch-target constants ────────────────────────────────────────────────────

_MOD = "app.pipeline.orchestrator"
_LOAD_SOURCES = f"{_MOD}._load_enabled_sources"
_GET_EVENTS = f"{_MOD}._get_raw_events"
_CONNECTOR = f"{_MOD}.USASpendingConnector"
_EXTRACT = f"{_MOD}.extract_evidence"
_RESOLVE = f"{_MOD}.resolve_company_for_evidence"
_SIGNALS = f"{_MOD}.detect_signals_for_evidence"
_SCORE = f"{_MOD}.score_company"


def _operational_error() -> OperationalError:
    return OperationalError("statement", {}, Exception("Software caused connection abort"))


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


# ─── Test 12b: gated company (scored=False) does not increment companies_scored ─


def test_gated_company_not_counted_in_companies_scored():
    """
    score_company returns {"scored": False} for gated companies. The orchestrator
    must NOT increment companies_scored in that case — otherwise the summary
    misleads callers into thinking leads were persisted when gates blocked them.
    """
    source = _make_source("usaspending")
    raw_event = _make_raw_event()
    evidence = _make_evidence()
    company = _make_company()

    with patch(_LOAD_SOURCES, return_value=[source]), \
         patch(_GET_EVENTS, return_value=[raw_event]), \
         patch(_CONNECTOR, return_value=MagicMock()), \
         patch(_EXTRACT, return_value=[evidence]), \
         patch(_RESOLVE, return_value=company), \
         patch(_SIGNALS, return_value=[_make_signal()]), \
         patch(_SCORE, return_value={"scored": False}):
        result = run_pipeline(_make_db())

    assert result["companies_scored"] == 0, (
        "gated company must not be counted in companies_scored"
    )
    assert result["companies_resolved"] == 1  # resolution still ran
    assert result["signals_created"] == 1     # signals still ran


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


# ─── Helpers for tier counter tests ──────────────────────────────────────────


def _run_with_tier(tier: str):
    """Run pipeline with a single event that scores to the given tier. Returns (result, pipeline_run_obj)."""
    source = _make_source("usaspending")
    raw_event = _make_raw_event()
    evidence = _make_evidence()
    company = _make_company()
    db = _make_db()

    with patch(_LOAD_SOURCES, return_value=[source]), \
         patch(_GET_EVENTS, return_value=[raw_event]), \
         patch(_CONNECTOR, return_value=MagicMock()), \
         patch(_EXTRACT, return_value=[evidence]), \
         patch(_RESOLVE, return_value=company), \
         patch(_SIGNALS, return_value=[_make_signal()]), \
         patch(_SCORE, return_value={"scored": True, "tier": tier}):
        result = run_pipeline(db)

    pipeline_run_obj = _find_added_obj(db, PipelineRun)
    return result, pipeline_run_obj


# ─── Test 13: warm lead increments total_warm ─────────────────────────────────


def test_warm_lead_increments_total_warm():
    result, pr = _run_with_tier("warm")
    assert result["total_warm"] == 1
    assert result["total_hot"] == 0
    assert result["total_cold"] == 0
    assert result["total_archive"] == 0
    assert pr.total_warm == 1
    assert pr.total_hot == 0


# ─── Test 14: cold lead increments total_cold ─────────────────────────────────


def test_cold_lead_increments_total_cold():
    result, pr = _run_with_tier("cold")
    assert result["total_cold"] == 1
    assert result["total_warm"] == 0
    assert result["total_hot"] == 0
    assert result["total_archive"] == 0
    assert pr.total_cold == 1


# ─── Test 15: hot lead increments total_hot ───────────────────────────────────


def test_hot_lead_increments_total_hot():
    result, pr = _run_with_tier("hot")
    assert result["total_hot"] == 1
    assert result["total_warm"] == 0
    assert result["total_cold"] == 0
    assert result["total_archive"] == 0
    assert pr.total_hot == 1


# ─── Test 16: archive lead increments total_archive ──────────────────────────


def test_archive_lead_increments_total_archive():
    result, pr = _run_with_tier("archive")
    assert result["total_archive"] == 1
    assert result["total_hot"] == 0
    assert result["total_warm"] == 0
    assert result["total_cold"] == 0
    assert pr.total_archive == 1


# ─── Test 17: gated lead does not increment any tier counter ──────────────────


def test_gated_lead_does_not_increment_tier_counters():
    source = _make_source("usaspending")
    raw_event = _make_raw_event()
    evidence = _make_evidence()
    company = _make_company()
    db = _make_db()

    with patch(_LOAD_SOURCES, return_value=[source]), \
         patch(_GET_EVENTS, return_value=[raw_event]), \
         patch(_CONNECTOR, return_value=MagicMock()), \
         patch(_EXTRACT, return_value=[evidence]), \
         patch(_RESOLVE, return_value=company), \
         patch(_SIGNALS, return_value=[_make_signal()]), \
         patch(_SCORE, return_value={"scored": False, "tier": None}):
        result = run_pipeline(db)

    assert result["total_hot"] == 0
    assert result["total_warm"] == 0
    assert result["total_cold"] == 0
    assert result["total_archive"] == 0
    assert result["companies_scored"] == 0

    pr = _find_added_obj(db, PipelineRun)
    assert pr.total_hot == 0
    assert pr.total_warm == 0
    assert pr.total_cold == 0
    assert pr.total_archive == 0


# ─── Test 18: quarantine_count from connector persisted to pipeline_run ───────


def test_quarantine_count_persisted_to_pipeline_run():
    source = _make_source("usaspending")
    db = _make_db()

    def fake_connector(db_arg, source_run_arg, source_arg):
        instance = MagicMock()
        def fake_run():
            source_run_arg.quarantine_count = 7
        instance.run.side_effect = fake_run
        return instance

    with patch(_LOAD_SOURCES, return_value=[source]), \
         patch(_GET_EVENTS, return_value=[]), \
         patch(_CONNECTOR, side_effect=fake_connector):
        result = run_pipeline(db)

    assert result["quarantine_count"] == 7
    pr = _find_added_obj(db, PipelineRun)
    assert pr.quarantine_count == 7


# ─── Batch-commit orphan-elimination tests ────────────────────────────────────
#
# Before this fix, evidence/resolution/signal/score work for an entire source
# run committed once at the very end of Step 3. A source with 262K raw events
# (already committed by its connector) could run for 22+ hours with zero
# commits — killing the process anywhere in that window permanently orphaned
# every one of those raw events (no evidence, no signals ever committed).
# These tests prove: (1) _commit_batch commits every batch independently,
# (2) a dropped connection mid-batch replays only that batch, and (3) a batch
# that fails after all retries leaves earlier, already-committed batches
# intact while contributing nothing to the summary itself — no partial/
# orphaned state is ever counted as done.


def test_commit_batch_commits_once_on_success():
    db = MagicMock()
    log = MagicMock()
    raw_events = [_make_raw_event(), _make_raw_event()]

    with patch(_EXTRACT, return_value=[]):
        batch_summary = _commit_batch(db, log, raw_events)

    assert db.commit.call_count == 1
    assert db.rollback.call_count == 0
    assert batch_summary["evidence_items_created"] == 0


def test_commit_batch_replays_batch_after_dropped_connection():
    """
    A dropped connection on the first commit attempt must roll back, then
    replay processing for the same batch (not skip it, not double-count it)
    before committing again.
    """
    db = MagicMock()
    db.commit.side_effect = [_operational_error(), None]
    log = MagicMock()
    raw_events = [_make_raw_event(), _make_raw_event()]

    extract_calls: list[uuid.UUID] = []

    def fake_extract(eid, db_arg):
        extract_calls.append(eid)
        return []

    with patch(_EXTRACT, side_effect=fake_extract), \
         patch(f"{_MOD}.time.sleep"):
        batch_summary = _commit_batch(db, log, raw_events)

    assert db.commit.call_count == 2
    assert db.rollback.call_count == 1
    # Processing replayed for the whole batch, not skipped or duplicated in the result.
    assert extract_calls == [raw_events[0].id, raw_events[1].id] * 2
    assert batch_summary["evidence_items_created"] == 0


def test_commit_batch_raises_after_retries_exhausted():
    db = MagicMock()
    db.commit.side_effect = lambda: (_ for _ in ()).throw(_operational_error())
    log = MagicMock()
    raw_events = [_make_raw_event()]

    with patch(_EXTRACT, return_value=[]), \
         patch(f"{_MOD}.time.sleep"):
        with pytest.raises(OperationalError):
            _commit_batch(db, log, raw_events)

    assert db.commit.call_count == MAX_RETRIES + 1
    assert db.rollback.call_count == MAX_RETRIES + 1


def test_interrupted_run_leaves_zero_orphans():
    """
    Simulate a source with two batches: the first batch commits successfully,
    the second batch's connection is permanently dead (every retry attempt
    fails). The run must end up "failed" for that source, but the summary
    must reflect exactly the first batch's committed work — nothing from the
    doomed second batch is ever counted, proving there is no window where a
    raw event's evidence/signal/score is credited without actually having
    been committed.
    """
    source = _make_source("usaspending")
    n_events = 2 * BATCH_COMMIT_SIZE
    raw_events = [_make_raw_event() for _ in range(n_events)]

    db = MagicMock()
    db.execute.return_value.scalar_one_or_none.return_value = None  # no stale run

    call_count = {"n": 0}

    def commit_side_effect():
        call_count["n"] += 1
        n = call_count["n"]
        # Calls: 1=pipeline_run create, 2=source_run create, 3=batch1 (ok),
        # 4..(4+MAX_RETRIES)=batch2 attempts (all fail), rest succeed.
        if 4 <= n <= 4 + MAX_RETRIES:
            raise _operational_error()

    db.commit.side_effect = commit_side_effect

    def fake_extract(eid, db_arg):
        return [_make_evidence()]

    def fake_resolve(eid, db_arg):
        return _make_company()

    def fake_signals(eid, db_arg):
        return [_make_signal()]

    with patch(_LOAD_SOURCES, return_value=[source]), \
         patch(_GET_EVENTS, return_value=raw_events), \
         patch(_CONNECTOR, return_value=MagicMock()), \
         patch(_EXTRACT, side_effect=fake_extract), \
         patch(_RESOLVE, side_effect=fake_resolve), \
         patch(_SIGNALS, side_effect=fake_signals), \
         patch(_SCORE, return_value={"scored": True, "tier": "warm"}), \
         patch(f"{_MOD}.time.sleep"):
        result = run_pipeline(db)

    assert result["status"] == "failed"
    assert result["sources_failed"] == 1
    assert result["sources_succeeded"] == 0
    # Only batch 1's work is reflected — batch 2 contributed nothing.
    assert result["raw_events_processed"] == BATCH_COMMIT_SIZE
    assert result["evidence_items_created"] == BATCH_COMMIT_SIZE
    assert result["companies_resolved"] == BATCH_COMMIT_SIZE
    assert result["signals_created"] == BATCH_COMMIT_SIZE
    assert result["companies_scored"] == BATCH_COMMIT_SIZE
    assert result["total_warm"] == BATCH_COMMIT_SIZE

    source_run_obj = _find_added_obj(db, SourceRun)
    assert source_run_obj.status == "failed"
