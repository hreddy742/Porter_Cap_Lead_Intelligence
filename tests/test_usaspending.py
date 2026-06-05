"""
Tests for the USASpending connector.

All five tests use mocked HTTP — no real network calls, no database container.
The session is a MagicMock configured per-test for dedup behaviour.

Counter semantics assumed by these tests:
  records_fetched  — every record received from the API (valid + invalid + deduped)
  records_valid    — stored in DB (passed validation, not a duplicate)
  records_skipped  — skipped because content_hash already exists
  quarantine_count — failed Pydantic validation
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.pipeline.connectors.usaspending import USASpendingConnector, USASpendingRecord


# ─── Shared helpers ───────────────────────────────────────────────────────────


def _make_source() -> MagicMock:
    src = MagicMock()
    src.id = uuid.uuid4()
    return src


def _make_source_run() -> MagicMock:
    run = MagicMock()
    run.id = uuid.uuid4()
    run.records_fetched = 0
    run.records_valid = 0
    run.records_skipped = 0
    run.quarantine_count = 0
    run.status = "running"
    run.error_text = None
    run.finished_at = None
    return run


def _make_session(*, record_exists: bool = False) -> MagicMock:
    """
    Returns a mock Session.
    record_exists=True  → dedup query always returns a truthy result (skip).
    record_exists=False → dedup query always returns None (store).
    """
    session = MagicMock()
    dedup_result = MagicMock() if record_exists else None
    session.execute.return_value.scalar_one_or_none.return_value = dedup_result
    return session


def _award(n: int) -> dict:
    """Minimal valid USASpending award payload."""
    return {
        "Award ID": f"CONT_AWD_{n:05d}",
        "Recipient Name": f"Acme Federal Services {n}",
        "Award Amount": float(50_000 * n),
        "Start Date": "2025-03-15",
        "Recipient UEI": f"UEI{n:09d}",
        "NAICS Code": "541511",
        "Place of Performance State Code": "VA",
        "Awarding Agency": "Dept of Defense",
    }


def _mock_response(results: list[dict], *, has_next: bool) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "limit": 100,
        "results": results,
        "page_metadata": {
            "page": 1,
            "hasNext": has_next,
            "last_record_unique_id": 99999,
            "last_record_sort_value": "50000",
        },
    }
    return resp


def _run_connector(
    *,
    responses: list[MagicMock],
    session: MagicMock | None = None,
    source: MagicMock | None = None,
    source_run: MagicMock | None = None,
) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Patch httpx.Client, run the connector, return (source_run, session, mock_client)."""
    if session is None:
        session = _make_session()
    if source is None:
        source = _make_source()
    if source_run is None:
        source_run = _make_source_run()

    with patch("app.pipeline.connectors.usaspending.httpx.Client") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value.__enter__.return_value = mock_client
        mock_client.post.side_effect = responses

        connector = USASpendingConnector(session, source_run, source, fiscal_year=2025)
        connector.run()

    return source_run, session, mock_client


# ─── Test 1: three pages, all fetched ────────────────────────────────────────


def test_three_pages_all_fetched():
    """
    When the API returns 3 pages (hasNext True, True, False), the connector
    makes exactly 3 HTTP calls and fetches all records from every page.
    """
    pages = [
        _mock_response([_award(1), _award(2)], has_next=True),
        _mock_response([_award(3), _award(4)], has_next=True),
        _mock_response([_award(5), _award(6)], has_next=False),
    ]

    source_run, _session, mock_client = _run_connector(responses=pages)

    assert mock_client.post.call_count == 3, "should make one HTTP call per page"
    assert source_run.records_fetched == 6
    assert source_run.records_valid == 6
    assert source_run.records_skipped == 0
    assert source_run.quarantine_count == 0
    assert source_run.status == "completed"


# ─── Test 2: timeout → source_run marked failed, no exception ────────────────


def test_timeout_marks_source_run_failed():
    """
    An httpx.TimeoutException causes source_run.status='failed' and
    source_run.error_text to be set. The connector must not propagate the exception.
    """
    timeout_exc = httpx.TimeoutException("connection timed out")

    source_run, _session, mock_client = _run_connector(
        responses=[timeout_exc],  # side_effect raises the exception
    )

    assert source_run.status == "failed", "status must be 'failed' after timeout"
    assert source_run.error_text is not None, "error_text must be set"
    assert len(source_run.error_text) > 0
    assert source_run.records_fetched == 0


# ─── Test 3: malformed record quarantined, others saved ──────────────────────


def test_malformed_record_quarantined_others_saved():
    """
    A batch with one invalid record (empty recipient_name) increments
    quarantine_count by 1 while the two valid records are stored normally.
    """
    bad_record = {
        "Award ID": "CONT_AWD_BAD01",
        "Recipient Name": "   ",  # blank after strip — validation failure
        "Award Amount": 75_000.0,
        "Start Date": "2025-03-15",
    }
    batch = [_award(10), bad_record, _award(11)]
    pages = [_mock_response(batch, has_next=False)]

    source_run, _session, _client = _run_connector(responses=pages)

    assert source_run.quarantine_count == 1, "malformed record must be quarantined"
    assert source_run.records_valid == 2, "two valid records must be stored"
    assert source_run.records_fetched == 3, "all three records were received from API"
    assert source_run.status == "completed"


# ─── Test 4: duplicate record skipped on second run ──────────────────────────


def test_duplicate_record_skipped():
    """
    Fetching the same record twice: the second run's connector sees the hash
    already present and increments records_skipped, not records_valid.
    """
    award = _award(42)

    # First run — DB has no existing record
    first_run, _s1, _c1 = _run_connector(
        responses=[_mock_response([award], has_next=False)],
        session=_make_session(record_exists=False),
    )
    assert first_run.records_valid == 1
    assert first_run.records_skipped == 0

    # Second run — same award, DB now reports content_hash already present
    second_run, _s2, _c2 = _run_connector(
        responses=[_mock_response([award], has_next=False)],
        session=_make_session(record_exists=True),
    )
    assert second_run.records_skipped == 1, "duplicate must be skipped"
    assert second_run.records_valid == 0, "skipped record must not be stored"
    assert second_run.records_fetched == 1, "record was still received from API"
    assert second_run.status == "completed"


# ─── Test 5: zero results → records_fetched stays 0 ─────────────────────────


def test_empty_results_completes_cleanly():
    """
    When the API returns an empty results list on page 1, the run completes
    with records_fetched=0 and status='completed'.
    """
    pages = [_mock_response([], has_next=False)]

    source_run, _session, mock_client = _run_connector(responses=pages)

    assert source_run.records_fetched == 0
    assert source_run.records_valid == 0
    assert source_run.status == "completed"
    assert mock_client.post.call_count == 1, "one API call should still be made"
