"""
Tests for app/ops/health.py

All tests use MagicMock sessions — no database required.
Functions under test are read-only: they must never call db.add, db.commit, db.delete.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.db.models import PipelineRun, SourceRegistry, SourceRun
from app.ops.health import (
    get_latest_pipeline_health,
    get_pipeline_run_summary,
    get_source_health,
)

# ─── Mock helpers ─────────────────────────────────────────────────────────────


def _run(**kw) -> MagicMock:
    r = MagicMock()
    r.id = kw.get("id", uuid4())
    r.started_at = kw.get("started_at", datetime(2024, 6, 1, 8, 0, tzinfo=timezone.utc))
    r.ended_at = kw.get("ended_at", None)
    r.status = kw.get("status", "completed")
    r.trigger = kw.get("trigger", "cron")
    r.total_records = kw.get("total_records", 0)
    r.total_hot = kw.get("total_hot", 0)
    r.total_warm = kw.get("total_warm", 0)
    r.total_cold = kw.get("total_cold", 0)
    r.total_archive = kw.get("total_archive", 0)
    r.quarantine_count = kw.get("quarantine_count", 0)
    r.error_summary = kw.get("error_summary", None)
    return r


def _source_run(**kw) -> MagicMock:
    sr = MagicMock()
    sr.id = kw.get("id", uuid4())
    sr.pipeline_run_id = kw.get("pipeline_run_id", uuid4())
    sr.source_id = kw.get("source_id", uuid4())
    sr.started_at = kw.get("started_at", datetime(2024, 6, 1, 8, 0, tzinfo=timezone.utc))
    sr.finished_at = kw.get("finished_at", None)
    sr.status = kw.get("status", "completed")
    sr.records_fetched = kw.get("records_fetched", 100)
    sr.records_valid = kw.get("records_valid", 90)
    sr.records_skipped = kw.get("records_skipped", 10)
    sr.quarantine_count = kw.get("quarantine_count", 0)
    sr.error_text = kw.get("error_text", None)
    return sr


def _source(**kw) -> MagicMock:
    s = MagicMock()
    s.id = kw.get("id", uuid4())
    s.name = kw.get("name", "USASpending")
    s.enabled = kw.get("enabled", True)
    s.status = kw.get("status", "enabled")
    s.last_success_at = kw.get("last_success_at", None)
    s.last_failure_at = kw.get("last_failure_at", None)
    return s


def _db_latest(pipeline_run, source_runs) -> MagicMock:
    """Mock db for get_latest_pipeline_health."""
    db = MagicMock()

    pipeline_q = MagicMock()
    pipeline_q.order_by.return_value.first.return_value = pipeline_run

    source_run_q = MagicMock()
    source_run_q.filter.return_value.all.return_value = source_runs

    def _side(model):
        if model is PipelineRun:
            return pipeline_q
        return source_run_q

    db.query.side_effect = _side
    return db


def _db_source_health(sources, runs_per_source) -> MagicMock:
    """
    Mock db for get_source_health.
    runs_per_source: list[list[MagicMock]] — one list of SourceRun mocks per source.
    db.query is called: first with SourceRegistry, then once per source with SourceRun.
    """
    db = MagicMock()

    source_q = MagicMock()
    source_q.order_by.return_value.all.return_value = sources

    source_run_qs = []
    for runs in runs_per_source:
        q = MagicMock()
        q.filter.return_value.order_by.return_value.limit.return_value.all.return_value = runs
        source_run_qs.append(q)

    db.query.side_effect = [source_q] + source_run_qs
    return db


def _db_summary(pipeline_run, source_runs) -> MagicMock:
    """Mock db for get_pipeline_run_summary."""
    db = MagicMock()

    pipeline_q = MagicMock()
    pipeline_q.filter.return_value.first.return_value = pipeline_run

    source_run_q = MagicMock()
    source_run_q.filter.return_value.all.return_value = source_runs

    db.query.side_effect = [pipeline_q, source_run_q]
    return db


# ─── Test 1: has_runs=False when no pipeline_runs exist ──────────────────────


def test_no_runs_returns_has_runs_false():
    db = _db_latest(pipeline_run=None, source_runs=[])
    result = get_latest_pipeline_health(db)

    assert result["has_runs"] is False
    assert result["pipeline_run_id"] is None
    assert result["status"] is None
    assert result["started_at"] is None
    assert result["finished_at"] is None
    assert result["duration_seconds"] is None
    assert result["sources_total"] == 0
    assert result["sources_succeeded"] == 0
    assert result["sources_failed"] == 0
    assert result["raw_events_processed"] is None
    assert result["evidence_items_created"] is None
    assert result["companies_resolved"] is None
    assert result["signals_created"] is None
    assert result["companies_scored"] is None
    assert result["errors"] == []


# ─── Test 2: completed run returns status and timestamps ─────────────────────


def test_completed_run_returns_status_and_timestamps():
    started = datetime(2024, 6, 1, 8, 0, tzinfo=timezone.utc)
    ended = datetime(2024, 6, 1, 9, 0, tzinfo=timezone.utc)
    run = _run(status="completed", started_at=started, ended_at=ended)
    db = _db_latest(pipeline_run=run, source_runs=[])
    result = get_latest_pipeline_health(db)

    assert result["has_runs"] is True
    assert result["pipeline_run_id"] == run.id
    assert result["status"] == "completed"
    assert result["started_at"] == started
    assert result["finished_at"] == ended


# ─── Test 3: duration_seconds computed from started_at and finished_at ───────


def test_duration_seconds_computed_when_both_timestamps_present():
    started = datetime(2024, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    ended = started + timedelta(seconds=90)
    run = _run(started_at=started, ended_at=ended)
    db = _db_latest(pipeline_run=run, source_runs=[])
    result = get_latest_pipeline_health(db)

    assert result["duration_seconds"] == 90.0


def test_duration_seconds_is_none_when_run_still_open():
    run = _run(started_at=datetime(2024, 6, 1, 10, 0, tzinfo=timezone.utc), ended_at=None)
    db = _db_latest(pipeline_run=run, source_runs=[])
    result = get_latest_pipeline_health(db)

    assert result["duration_seconds"] is None


# ─── Test 4: failed source_runs appear in errors ─────────────────────────────


def test_failed_source_run_appears_in_errors():
    run = _run()
    failed_sr = _source_run(status="failed", error_text="Connection timeout after 30s")
    db = _db_latest(pipeline_run=run, source_runs=[failed_sr])
    result = get_latest_pipeline_health(db)

    assert result["sources_total"] == 1
    assert result["sources_failed"] == 1
    assert result["sources_succeeded"] == 0
    assert len(result["errors"]) == 1
    assert result["errors"][0]["error_text"] == "Connection timeout after 30s"
    assert result["errors"][0]["source_run_id"] == str(failed_sr.id)


def test_successful_source_run_not_in_errors():
    run = _run()
    ok_sr = _source_run(status="completed")
    db = _db_latest(pipeline_run=run, source_runs=[ok_sr])
    result = get_latest_pipeline_health(db)

    assert result["sources_succeeded"] == 1
    assert result["errors"] == []


# ─── Test 5: get_source_health returns enabled and disabled sources ───────────


def test_source_health_returns_enabled_and_disabled_sources():
    s1 = _source(name="AAA", enabled=True, status="enabled")
    s2 = _source(name="BBB", enabled=False, status="planned")
    db = _db_source_health([s1, s2], [[], []])
    result = get_source_health(db)

    assert len(result) == 2
    by_name = {r["name"]: r for r in result}
    assert by_name["AAA"]["enabled"] is True
    assert by_name["BBB"]["enabled"] is False
    assert by_name["BBB"]["status"] == "planned"


# ─── Test 6: get_source_health includes last_success_at and last_failure_at ──


def test_source_health_includes_last_success_and_failure_timestamps():
    success_ts = datetime(2024, 6, 1, 8, 0, tzinfo=timezone.utc)
    failure_ts = datetime(2024, 6, 2, 8, 0, tzinfo=timezone.utc)
    s = _source(last_success_at=success_ts, last_failure_at=failure_ts)
    db = _db_source_health([s], [[]])
    result = get_source_health(db)

    assert len(result) == 1
    assert result[0]["last_success_at"] == success_ts
    assert result[0]["last_failure_at"] == failure_ts


def test_source_health_last_error_is_none_when_no_failures():
    s = _source()
    ok_sr = _source_run(status="completed")
    db = _db_source_health([s], [[ok_sr]])
    result = get_source_health(db)

    assert result[0]["last_error"] is None


def test_source_health_last_error_from_most_recent_failed_run():
    s = _source()
    failed_sr = _source_run(
        status="failed",
        error_text="API rate limit",
        started_at=datetime(2024, 6, 3, 8, 0, tzinfo=timezone.utc),
    )
    ok_sr = _source_run(
        status="completed",
        started_at=datetime(2024, 6, 2, 8, 0, tzinfo=timezone.utc),
    )
    # recent_runs is ordered newest-first: failed_sr is first
    db = _db_source_health([s], [[failed_sr, ok_sr]])
    result = get_source_health(db)

    assert result[0]["last_error"] == "API rate limit"


# ─── Test 7: get_source_health includes recent_runs ─────────────────────────


def test_source_health_includes_recent_source_runs():
    s = _source()
    sr1 = _source_run(status="completed", records_fetched=200)
    sr2 = _source_run(status="completed", records_fetched=150)
    db = _db_source_health([s], [[sr1, sr2]])
    result = get_source_health(db)

    assert len(result[0]["recent_runs"]) == 2
    assert result[0]["recent_runs"][0]["records_fetched"] == 200
    assert result[0]["recent_runs"][1]["records_fetched"] == 150


def test_source_health_empty_recent_runs_when_no_source_runs():
    s = _source()
    db = _db_source_health([s], [[]])
    result = get_source_health(db)

    assert result[0]["recent_runs"] == []


# ─── Test 8: get_pipeline_run_summary returns None for missing run ────────────


def test_pipeline_run_summary_returns_none_for_unknown_id():
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None

    result = get_pipeline_run_summary(uuid4(), db)

    assert result is None


# ─── Test 9: get_pipeline_run_summary returns source_runs for existing run ───


def test_pipeline_run_summary_returns_source_runs():
    run_id = uuid4()
    run = _run(id=run_id, status="completed")
    sr_ok = _source_run(pipeline_run_id=run_id, status="completed", records_fetched=300)
    sr_fail = _source_run(
        pipeline_run_id=run_id, status="failed", error_text="Upstream 503"
    )
    db = _db_summary(pipeline_run=run, source_runs=[sr_ok, sr_fail])

    result = get_pipeline_run_summary(run_id, db)

    assert result is not None
    assert result["pipeline_run_id"] == run_id
    assert result["status"] == "completed"
    assert len(result["source_runs"]) == 2
    assert len(result["errors"]) == 1
    assert result["errors"][0]["error_text"] == "Upstream 503"


def test_pipeline_run_summary_duration_computed():
    started = datetime(2024, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    ended = started + timedelta(seconds=120)
    run = _run(started_at=started, ended_at=ended)
    db = _db_summary(pipeline_run=run, source_runs=[])

    result = get_pipeline_run_summary(run.id, db)

    assert result["duration_seconds"] == 120.0


def test_pipeline_run_summary_source_run_fields_present():
    run = _run()
    sr = _source_run(records_fetched=50, records_valid=45, records_skipped=5)
    db = _db_summary(pipeline_run=run, source_runs=[sr])

    result = get_pipeline_run_summary(run.id, db)

    sr_out = result["source_runs"][0]
    assert sr_out["records_fetched"] == 50
    assert sr_out["records_valid"] == 45
    assert sr_out["records_skipped"] == 5
    assert sr_out["status"] == "completed"


# ─── Test 10: functions are read-only ────────────────────────────────────────


def test_get_latest_pipeline_health_is_read_only():
    db = _db_latest(pipeline_run=None, source_runs=[])
    get_latest_pipeline_health(db)
    db.add.assert_not_called()
    db.commit.assert_not_called()
    db.delete.assert_not_called()
    db.execute.assert_not_called()


def test_get_source_health_is_read_only():
    db = _db_source_health([], [])
    get_source_health(db)
    db.add.assert_not_called()
    db.commit.assert_not_called()
    db.delete.assert_not_called()


def test_get_pipeline_run_summary_is_read_only():
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    get_pipeline_run_summary(uuid4(), db)
    db.add.assert_not_called()
    db.commit.assert_not_called()
    db.delete.assert_not_called()


# ─── Test 11: no external API calls ──────────────────────────────────────────


def test_no_external_api_calls():
    import app.ops.health as health_module

    # Confirm network libraries are not imported at module level
    assert not hasattr(health_module, "httpx"), "httpx must not be imported in health.py"
    assert not hasattr(health_module, "requests"), "requests must not be imported in health.py"

    # All three functions must complete successfully on mock data
    db1 = _db_latest(pipeline_run=None, source_runs=[])
    result1 = get_latest_pipeline_health(db1)
    assert isinstance(result1, dict)

    db2 = _db_source_health([], [])
    result2 = get_source_health(db2)
    assert isinstance(result2, list)

    db3 = MagicMock()
    db3.query.return_value.filter.return_value.first.return_value = None
    result3 = get_pipeline_run_summary(uuid4(), db3)
    assert result3 is None
