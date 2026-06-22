"""
Tests for the USASpending subawards connector.

All tests use mocked HTTP — no real network calls, no database container.
The session is a MagicMock configured per-test for dedup behaviour.

Counter semantics (mirrors test_usaspending.py):
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

from app.pipeline.connectors.usaspending_subawards import (
    USASpendingSubawardsConnector,
    USASpendingSubawardsRecord,
)


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
    session = MagicMock()
    dedup_result = MagicMock() if record_exists else None
    session.execute.return_value.scalar_one_or_none.return_value = dedup_result
    return session


def _subaward(n: int) -> dict:
    """Minimal valid USASpending subaward payload (lowercase field names)."""
    return {
        "id": 1000000 + n,
        "subaward_number": f"SUB-{n:05d}",
        "description": f"MANUFACTURING SERVICES ORDER {n}",
        "action_date": "2025-06-15",
        "amount": float(75_000 * n),
        "recipient_name": f"Acme Manufacturing LLC {n}",
    }


def _mock_response(results: list[dict], *, has_next: bool) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "page_metadata": {
            "page": 1,
            "next": 2 if has_next else None,
            "previous": None,
            "hasNext": has_next,
            "hasPrevious": False,
        },
        "results": results,
    }
    return resp


def _run_connector(
    *,
    responses: list[MagicMock],
    session: MagicMock | None = None,
    source: MagicMock | None = None,
    source_run: MagicMock | None = None,
    env: dict[str, str] | None = None,
) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Patch httpx.Client, run the connector, return (source_run, session, mock_client).

    Always resets USASPENDING_SUBAWARDS_* env vars so host-shell exports don't leak.
    Pass env= to override specific vars for tests that need non-default values.
    """
    if session is None:
        session = _make_session()
    if source is None:
        source = _make_source()
    if source_run is None:
        source_run = _make_source_run()

    env_patch = {
        "USASPENDING_SUBAWARDS_MAX_PAGES": "",
        "USASPENDING_SUBAWARDS_PAGE_LIMIT": "",
        "USASPENDING_SUBAWARDS_TIMEOUT_SECONDS": "",
        "USASPENDING_SUBAWARDS_MAX_RETRIES": "",
        "USASPENDING_SUBAWARDS_BACKOFF_BASE_SECONDS": "",
        "USASPENDING_SUBAWARDS_BACKOFF_MAX_SECONDS": "",
    }
    if env:
        env_patch.update(env)

    with patch("app.pipeline.connectors.usaspending_subawards.httpx.Client") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value.__enter__.return_value = mock_client
        mock_client.post.side_effect = responses

        with patch("app.pipeline.connectors.usaspending_subawards.time.sleep"):
            with patch(
                "app.pipeline.connectors.usaspending_subawards.random.uniform",
                return_value=0.0,
            ):
                with patch.dict("os.environ", env_patch):
                    connector = USASpendingSubawardsConnector(session, source_run, source)
                    connector.run()

    return source_run, session, mock_client


# ─── Test 1: three pages all fetched ─────────────────────────────────────────


def test_three_pages_all_fetched():
    """
    When the API returns 3 pages (hasNext True, True, False), the connector
    makes exactly 3 HTTP calls and stores all records.
    """
    pages = [
        _mock_response([_subaward(1), _subaward(2)], has_next=True),
        _mock_response([_subaward(3), _subaward(4)], has_next=True),
        _mock_response([_subaward(5), _subaward(6)], has_next=False),
    ]

    source_run, _session, mock_client = _run_connector(responses=pages)

    assert mock_client.post.call_count == 3
    assert source_run.records_fetched == 6
    assert source_run.records_valid == 6
    assert source_run.records_skipped == 0
    assert source_run.quarantine_count == 0
    assert source_run.status == "completed"


# ─── Test 2: empty response completes cleanly ─────────────────────────────────


def test_empty_results_completes_cleanly():
    """Empty results list on page 1 → records_fetched=0, status='completed'."""
    pages = [_mock_response([], has_next=False)]

    source_run, _session, mock_client = _run_connector(responses=pages)

    assert source_run.records_fetched == 0
    assert source_run.records_valid == 0
    assert source_run.status == "completed"
    assert mock_client.post.call_count == 1


# ─── Test 3: zero/negative amount quarantined ────────────────────────────────


def test_zero_amount_quarantined():
    """A record with amount=0 fails validation and increments quarantine_count."""
    bad = {**_subaward(1), "amount": 0.0}
    pages = [_mock_response([bad], has_next=False)]

    source_run, _session, _client = _run_connector(responses=pages)

    assert source_run.quarantine_count == 1
    assert source_run.records_valid == 0
    assert source_run.records_fetched == 1


def test_negative_amount_quarantined():
    """A record with amount < 0 fails validation and increments quarantine_count."""
    bad = {**_subaward(1), "amount": -5000.0}
    pages = [_mock_response([bad], has_next=False)]

    source_run, _session, _client = _run_connector(responses=pages)

    assert source_run.quarantine_count == 1
    assert source_run.records_valid == 0


# ─── Test 4: amount above $1B cap quarantined ────────────────────────────────


def test_corrupt_amount_above_cap_quarantined():
    """
    Amounts above $1B are quarantined as data corruption.
    The real subawards dataset contains trillion-dollar rows (e.g. $39T radar antenna).
    """
    corrupt = {**_subaward(1), "amount": 39_157_943_915_794.0}
    valid = _subaward(2)
    pages = [_mock_response([corrupt, valid], has_next=False)]

    source_run, _session, _client = _run_connector(responses=pages)

    assert source_run.quarantine_count == 1, "corrupt record must be quarantined"
    assert source_run.records_valid == 1, "valid record must still be stored"
    assert source_run.records_fetched == 2


# ─── Test 5: corrupt date (year 6010) quarantined ────────────────────────────


def test_corrupt_future_year_date_quarantined():
    """
    action_date year outside [2000, 2030] is quarantined.
    The real subawards dataset has rows with action_date = '6010-11-01'.
    """
    corrupt = {**_subaward(1), "action_date": "6010-11-01"}
    valid = _subaward(2)
    pages = [_mock_response([corrupt, valid], has_next=False)]

    source_run, _session, _client = _run_connector(responses=pages)

    assert source_run.quarantine_count == 1, "year-6010 date must be quarantined"
    assert source_run.records_valid == 1, "valid record must still be stored"


def test_corrupt_past_year_date_quarantined():
    """action_date year before 2000 is also quarantined."""
    corrupt = {**_subaward(1), "action_date": "1985-06-01"}
    pages = [_mock_response([corrupt], has_next=False)]

    source_run, _session, _client = _run_connector(responses=pages)

    assert source_run.quarantine_count == 1


# ─── Test 6: empty recipient_name quarantined ────────────────────────────────


def test_blank_recipient_name_quarantined():
    """A record with blank recipient_name must be quarantined."""
    bad = {**_subaward(10), "recipient_name": "   "}
    batch = [_subaward(11), bad, _subaward(12)]
    pages = [_mock_response(batch, has_next=False)]

    source_run, _session, _client = _run_connector(responses=pages)

    assert source_run.quarantine_count == 1
    assert source_run.records_valid == 2
    assert source_run.records_fetched == 3


# ─── Test 7: content-hash deduplication ──────────────────────────────────────


def test_duplicate_record_skipped():
    """Same record seen twice: second run increments records_skipped, not records_valid."""
    award = _subaward(42)

    first_run, _s1, _c1 = _run_connector(
        responses=[_mock_response([award], has_next=False)],
        session=_make_session(record_exists=False),
    )
    assert first_run.records_valid == 1
    assert first_run.records_skipped == 0

    second_run, _s2, _c2 = _run_connector(
        responses=[_mock_response([award], has_next=False)],
        session=_make_session(record_exists=True),
    )
    assert second_run.records_skipped == 1
    assert second_run.records_valid == 0
    assert second_run.records_fetched == 1
    assert second_run.status == "completed"


# ─── Test 8: USASPENDING_SUBAWARDS_MAX_PAGES=1 stops after one page ──────────


def test_max_pages_env_var_stops_after_one_page():
    """USASPENDING_SUBAWARDS_MAX_PAGES=1 stops after page 1 even when hasNext=True."""
    pages = [
        _mock_response([_subaward(1), _subaward(2)], has_next=True),
        _mock_response([_subaward(3), _subaward(4)], has_next=False),
    ]

    source_run, _session, mock_client = _run_connector(
        responses=pages, env={"USASPENDING_SUBAWARDS_MAX_PAGES": "1"}
    )

    assert mock_client.post.call_count == 1
    assert source_run.records_fetched == 2
    assert source_run.status == "completed"


# ─── Test 9: USASPENDING_SUBAWARDS_PAGE_LIMIT sent in request body ───────────


def test_page_limit_env_var_sent_in_request():
    """USASPENDING_SUBAWARDS_PAGE_LIMIT=10 must appear as limit=10 in the POST body."""
    pages = [_mock_response([_subaward(1)], has_next=False)]

    _source_run, _session, mock_client = _run_connector(
        responses=pages, env={"USASPENDING_SUBAWARDS_PAGE_LIMIT": "10"}
    )

    call_kwargs = mock_client.post.call_args
    sent_body = call_kwargs.kwargs.get("json") or call_kwargs.args[1]
    assert sent_body["limit"] == 10


# ─── Test 10: sort by id desc in request body ────────────────────────────────


def test_sort_by_id_desc_in_request_body():
    """
    The connector must sort by 'id' descending — not 'action_date'.
    action_date sort returns corrupted year-6010 dates at the top.
    """
    pages = [_mock_response([_subaward(1)], has_next=False)]

    _source_run, _session, mock_client = _run_connector(responses=pages)

    call_kwargs = mock_client.post.call_args
    sent_body = call_kwargs.kwargs.get("json") or call_kwargs.args[1]
    assert sent_body.get("sort") == "id", "must sort by id, not action_date"
    assert sent_body.get("order") == "desc"


# ─── Test 11: correct endpoint URL ───────────────────────────────────────────


def test_request_targets_subawards_endpoint():
    """The connector must POST to /api/v2/subawards/, not spending_by_transaction."""
    from app.pipeline.connectors.usaspending_subawards import _ENDPOINT

    assert "subawards" in _ENDPOINT
    assert "spending_by_transaction" not in _ENDPOINT
    assert "spending_by_award" not in _ENDPOINT


# ─── Test 12: source_url contains record id ──────────────────────────────────


def test_source_url_contains_record_id():
    """RawSourceEvent.source_url must reference the record's id field."""
    award_data = _subaward(99)
    pages = [_mock_response([award_data], has_next=False)]

    _source_run, session, _client = _run_connector(responses=pages)

    added = session.add.call_args[0][0]
    assert str(award_data["id"]) in added.source_url, (
        "source_url must embed the subaward record id"
    )


# ─── Test 13: source_record_id stored as string ──────────────────────────────


def test_source_record_id_stored_as_string():
    """source_record_id must be the str() of the integer id field."""
    award_data = _subaward(77)
    pages = [_mock_response([award_data], has_next=False)]

    _source_run, session, _client = _run_connector(responses=pages)

    added = session.add.call_args[0][0]
    assert added.source_record_id == str(award_data["id"])


# ─── Test 14: timeout marks source_run failed ────────────────────────────────


def test_timeout_marks_source_run_failed():
    """httpx.TimeoutException → status='failed', error_text set, no exception propagated."""
    source_run, _session, _client = _run_connector(
        responses=[httpx.TimeoutException("timed out")],
        env={"USASPENDING_SUBAWARDS_MAX_RETRIES": "0"},
    )

    assert source_run.status == "failed"
    assert source_run.error_text is not None
    assert len(source_run.error_text) > 0
    assert source_run.records_fetched == 0


# ─── Test 15: HTTP 429 retries then succeeds ─────────────────────────────────


def _retryable_response(status_code: int) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    return resp


def _permanent_error_response(status_code: int) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        f"HTTP {status_code}", request=MagicMock(), response=resp
    )
    return resp


def test_http_429_retries_then_succeeds():
    """A 429 on the first attempt is retried; the second attempt succeeds."""
    responses = [
        _retryable_response(429),
        _mock_response([_subaward(1)], has_next=False),
    ]

    source_run, _session, mock_client = _run_connector(
        responses=responses, env={"USASPENDING_SUBAWARDS_MAX_RETRIES": "3"}
    )

    assert mock_client.post.call_count == 2
    assert source_run.status == "completed"
    assert source_run.records_fetched == 1


# ─── Test 16: HTTP 500 retries then succeeds ─────────────────────────────────


def test_http_500_retries_then_succeeds():
    """A 500 on the first attempt is retried; the second attempt succeeds."""
    responses = [
        _retryable_response(500),
        _mock_response([_subaward(2)], has_next=False),
    ]

    source_run, _session, mock_client = _run_connector(
        responses=responses, env={"USASPENDING_SUBAWARDS_MAX_RETRIES": "3"}
    )

    assert mock_client.post.call_count == 2
    assert source_run.status == "completed"


# ─── Test 17: HTTP 400 does not retry ────────────────────────────────────────


def test_http_400_does_not_retry():
    """A 400 is permanent — exactly 1 HTTP call and status='failed'."""
    source_run, _session, mock_client = _run_connector(
        responses=[_permanent_error_response(400)]
    )

    assert mock_client.post.call_count == 1
    assert source_run.status == "failed"


# ─── Test 18: network timeout retries then succeeds ──────────────────────────


def test_network_timeout_retries_then_succeeds():
    """httpx.TimeoutException on first attempt is retried; second succeeds."""
    responses = [
        httpx.TimeoutException("read timed out"),
        _mock_response([_subaward(3)], has_next=False),
    ]

    source_run, _session, mock_client = _run_connector(
        responses=responses, env={"USASPENDING_SUBAWARDS_MAX_RETRIES": "3"}
    )

    assert mock_client.post.call_count == 2
    assert source_run.status == "completed"
    assert source_run.records_fetched == 1


# ─── Test 19: ConnectError retries then succeeds ─────────────────────────────


def test_connect_error_retries_then_succeeds():
    """httpx.ConnectError on first attempt is retried; second succeeds."""
    responses = [
        httpx.ConnectError("connection refused"),
        _mock_response([_subaward(4)], has_next=False),
    ]

    source_run, _session, mock_client = _run_connector(
        responses=responses, env={"USASPENDING_SUBAWARDS_MAX_RETRIES": "3"}
    )

    assert mock_client.post.call_count == 2
    assert source_run.status == "completed"


# ─── Test 20: retries exhausted → ConnectorError ─────────────────────────────


def test_retries_exhausted_marks_source_run_failed():
    """All attempts fail → status='failed', error_text contains 'subawards'."""
    responses = [_retryable_response(500)] * 4  # 1 initial + 3 retries

    source_run, _session, mock_client = _run_connector(
        responses=responses, env={"USASPENDING_SUBAWARDS_MAX_RETRIES": "3"}
    )

    assert mock_client.post.call_count == 4
    assert source_run.status == "failed"
    assert source_run.error_text is not None
    assert "subawards" in source_run.error_text.lower()


# ─── Test 21: RemoteProtocolError retries then succeeds ──────────────────────


def test_remote_protocol_error_retries_then_succeeds():
    """httpx.RemoteProtocolError on first attempt is retried; second succeeds."""
    responses = [
        httpx.RemoteProtocolError("peer closed connection"),
        _mock_response([_subaward(5)], has_next=False),
    ]

    source_run, _session, mock_client = _run_connector(
        responses=responses, env={"USASPENDING_SUBAWARDS_MAX_RETRIES": "3"}
    )

    assert mock_client.post.call_count == 2
    assert source_run.status == "completed"
    assert source_run.records_fetched == 1


# ─── Test 22: default max_pages is 200 ───────────────────────────────────────


def test_default_max_pages_is_200_when_env_var_absent():
    """When USASPENDING_SUBAWARDS_MAX_PAGES is unset, max_pages defaults to 200."""
    with patch.dict(
        "os.environ",
        {
            "USASPENDING_SUBAWARDS_MAX_PAGES": "",
            "USASPENDING_SUBAWARDS_PAGE_LIMIT": "",
            "USASPENDING_SUBAWARDS_TIMEOUT_SECONDS": "",
            "USASPENDING_SUBAWARDS_MAX_RETRIES": "",
            "USASPENDING_SUBAWARDS_BACKOFF_BASE_SECONDS": "",
            "USASPENDING_SUBAWARDS_BACKOFF_MAX_SECONDS": "",
        },
    ):
        connector = USASpendingSubawardsConnector(
            _make_session(), _make_source_run(), _make_source()
        )

    assert connector.max_pages == 200


# ─── Test 23: env var override for max_pages ─────────────────────────────────


def test_max_pages_env_var_override_respected():
    """USASPENDING_SUBAWARDS_MAX_PAGES=5 overrides the 200 default."""
    with patch.dict(
        "os.environ",
        {
            "USASPENDING_SUBAWARDS_MAX_PAGES": "5",
            "USASPENDING_SUBAWARDS_PAGE_LIMIT": "",
            "USASPENDING_SUBAWARDS_TIMEOUT_SECONDS": "",
            "USASPENDING_SUBAWARDS_MAX_RETRIES": "",
            "USASPENDING_SUBAWARDS_BACKOFF_BASE_SECONDS": "",
            "USASPENDING_SUBAWARDS_BACKOFF_MAX_SECONDS": "",
        },
    ):
        connector = USASpendingSubawardsConnector(
            _make_session(), _make_source_run(), _make_source()
        )

    assert connector.max_pages == 5


# ─── Test 24: timeout env var passed to httpx.Client ─────────────────────────


def test_timeout_env_var_passed_to_client():
    """USASPENDING_SUBAWARDS_TIMEOUT_SECONDS=45 → httpx.Client constructed with timeout=45.0."""
    pages = [_mock_response([_subaward(1)], has_next=False)]

    with patch("app.pipeline.connectors.usaspending_subawards.httpx.Client") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value.__enter__.return_value = mock_client
        mock_client.post.side_effect = pages

        with patch("app.pipeline.connectors.usaspending_subawards.time.sleep"):
            with patch(
                "app.pipeline.connectors.usaspending_subawards.random.uniform",
                return_value=0.0,
            ):
                with patch.dict("os.environ", {
                    "USASPENDING_SUBAWARDS_TIMEOUT_SECONDS": "45",
                    "USASPENDING_SUBAWARDS_MAX_PAGES": "",
                    "USASPENDING_SUBAWARDS_PAGE_LIMIT": "",
                    "USASPENDING_SUBAWARDS_MAX_RETRIES": "",
                    "USASPENDING_SUBAWARDS_BACKOFF_BASE_SECONDS": "",
                    "USASPENDING_SUBAWARDS_BACKOFF_MAX_SECONDS": "",
                }):
                    connector = USASpendingSubawardsConnector(
                        _make_session(), _make_source_run(), _make_source()
                    )
                    connector.run()

    mock_cls.assert_called_once_with(timeout=45.0)


# ─── Test 25: valid boundary amounts pass through ────────────────────────────


def test_boundary_amounts_pass_validation():
    """Amounts at $30K and $999M (just below the $1B cap) must pass."""
    record_30k = {**_subaward(1), "amount": 30_000.0}
    record_999m = {**_subaward(2), "amount": 999_999_999.0}
    pages = [_mock_response([record_30k, record_999m], has_next=False)]

    source_run, _session, _client = _run_connector(responses=pages)

    assert source_run.records_valid == 2
    assert source_run.quarantine_count == 0


# ─── Test 26: valid boundary date years pass through ─────────────────────────


def test_boundary_date_years_pass_validation():
    """Dates at exactly 2000-01-01 and 2030-12-31 (inclusive bounds) must pass."""
    record_2000 = {**_subaward(1), "action_date": "2000-01-01"}
    record_2030 = {**_subaward(2), "action_date": "2030-12-31"}
    pages = [_mock_response([record_2000, record_2030], has_next=False)]

    source_run, _session, _client = _run_connector(responses=pages)

    assert source_run.records_valid == 2
    assert source_run.quarantine_count == 0


# ─── Test 27: USASpendingSubawardsRecord model validation ────────────────────


def test_record_model_valid_payload():
    """A fully valid payload must parse without error."""
    record = USASpendingSubawardsRecord.model_validate(_subaward(1))
    assert record.recipient_name == "Acme Manufacturing LLC 1"
    assert record.award_amount > 0
    assert record.record_id == 1_000_001


def test_record_model_strips_whitespace_from_name():
    """Leading/trailing whitespace in recipient_name must be stripped."""
    raw = {**_subaward(1), "recipient_name": "  Acme Corp  "}
    record = USASpendingSubawardsRecord.model_validate(raw)
    assert record.recipient_name == "Acme Corp"


def test_record_model_rejects_empty_name():
    """An empty recipient_name must raise ValueError."""
    import pytest
    from pydantic import ValidationError
    raw = {**_subaward(1), "recipient_name": ""}
    with pytest.raises(ValidationError):
        USASpendingSubawardsRecord.model_validate(raw)


def test_record_model_rejects_amount_above_cap():
    """An amount above $1B must raise ValueError."""
    from pydantic import ValidationError
    raw = {**_subaward(1), "amount": 2_000_000_000.0}
    with pytest.raises(ValidationError):
        USASpendingSubawardsRecord.model_validate(raw)


def test_record_model_rejects_year_6010_date():
    """A date with year 6010 must raise ValueError."""
    from pydantic import ValidationError
    raw = {**_subaward(1), "action_date": "6010-11-01"}
    with pytest.raises(ValidationError):
        USASpendingSubawardsRecord.model_validate(raw)
