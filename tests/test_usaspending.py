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
    """Minimal valid USASpending transaction payload (spending_by_transaction field names)."""
    return {
        "Award ID": f"CONT_AWD_{n:05d}",
        "generated_internal_id": f"CONT_AWD_{n:05d}_9700_-NONE-_-NONE-",
        "Recipient Name": f"Acme Federal Services {n}",
        "Transaction Amount": float(50_000 * n),
        "Action Date": "2025-03-15",
        "Recipient UEI": f"UEI{n:09d}",
        "naics_code": "541511",
        "naics_description": "Custom Computer Programming Services",
        "pop_state_code": "VA",
        "Awarding Agency": "Dept of Defense",
    }


def _mock_response(results: list[dict], *, has_next: bool) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
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
    env: dict[str, str] | None = None,
) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Patch httpx.Client, run the connector, return (source_run, session, mock_client).

    Always resets USASPENDING_* env vars so host-shell exports don't leak into tests.
    Pass env= to override specific vars for tests that need non-default values.
    """
    if session is None:
        session = _make_session()
    if source is None:
        source = _make_source()
    if source_run is None:
        source_run = _make_source_run()

    # Clear env vars that could leak from the host shell; callers override via env=
    env_patch = {
        "USASPENDING_MAX_PAGES": "",
        "USASPENDING_PAGE_LIMIT": "",
        "USASPENDING_TIMEOUT_SECONDS": "",
        "USASPENDING_MAX_RETRIES": "",
        "USASPENDING_BACKOFF_BASE_SECONDS": "",
        "USASPENDING_BACKOFF_MAX_SECONDS": "",
    }
    if env:
        env_patch.update(env)

    with patch("app.pipeline.connectors.usaspending.httpx.Client") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value.__enter__.return_value = mock_client
        mock_client.post.side_effect = responses

        with patch("app.pipeline.connectors.usaspending.time.sleep"):
            with patch(
                "app.pipeline.connectors.usaspending.random.uniform", return_value=0.0
            ):
                with patch.dict("os.environ", env_patch):
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
        env={"USASPENDING_MAX_RETRIES": "0"},  # disable retries so one timeout = immediate fail
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
        "Transaction Amount": 75_000.0,
        "Action Date": "2025-03-15",
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


# ─── Test 6: USASPENDING_MAX_PAGES=1 stops after one page ────────────────────


def test_max_pages_env_var_stops_after_one_page():
    """
    When USASPENDING_MAX_PAGES=1, the connector stops after the first page
    even if hasNext=True, making exactly one HTTP call.
    """
    pages = [
        _mock_response([_award(1), _award(2)], has_next=True),
        _mock_response([_award(3), _award(4)], has_next=False),
    ]

    source_run, _session, mock_client = _run_connector(
        responses=pages, env={"USASPENDING_MAX_PAGES": "1"}
    )

    assert mock_client.post.call_count == 1, "should stop after one page"
    assert source_run.records_fetched == 2
    assert source_run.records_valid == 2
    assert source_run.status == "completed"


# ─── Test 7: USASPENDING_PAGE_LIMIT=10 sent in request body ──────────────────


def test_page_limit_env_var_sent_in_request():
    """
    When USASPENDING_PAGE_LIMIT=10, the connector sends limit=10 in the
    POST body instead of the default 100.
    """
    pages = [_mock_response([_award(1)], has_next=False)]

    _source_run, _session, mock_client = _run_connector(
        responses=pages, env={"USASPENDING_PAGE_LIMIT": "10"}
    )

    call_kwargs = mock_client.post.call_args
    sent_body = call_kwargs.kwargs.get("json") or call_kwargs.args[1]
    assert sent_body["limit"] == 10, "request body must reflect the env var page limit"


# ─── Test 8: NAICS filter removed from request body ──────────────────────────


def test_naics_filter_not_sent_in_request_body():
    """
    The connector must NOT send naics_codes in filters — all sectors are
    collected now; gates.py handles soft-flagging of excluded sectors instead
    of this connector hard-blocking them by never fetching them. Fixed per
    John Cox Miller, Porter Capital, July 2026.
    """
    pages = [_mock_response([_award(1)], has_next=False)]

    _source_run, _session, mock_client = _run_connector(responses=pages)

    call_kwargs = mock_client.post.call_args
    sent_body = call_kwargs.kwargs.get("json") or call_kwargs.args[1]
    filters = sent_body.get("filters", {})
    assert "naics_codes" not in filters, "naics_codes filter must be removed"


# ─── Test 9: Action Date sort sent in request body ───────────────────────────


def test_action_date_sort_sent_in_request_body():
    """
    Sorting by Action Date (real obligation date) ensures most-recent
    contracts are returned first, producing accurate freshness scores.
    """
    pages = [_mock_response([_award(1)], has_next=False)]

    _source_run, _session, mock_client = _run_connector(responses=pages)

    call_kwargs = mock_client.post.call_args
    sent_body = call_kwargs.kwargs.get("json") or call_kwargs.args[1]
    assert sent_body.get("sort") == "Action Date", (
        "request must sort by 'Action Date' — not 'Start Date'"
    )


# ─── Test 10: spending_by_transaction endpoint URL ────────────────────────────


def test_request_targets_spending_by_transaction_endpoint():
    """
    The connector must POST to spending_by_transaction, not spending_by_award.
    spending_by_award returns NAICS Code = null for most DoD contracts.
    """
    pages = [_mock_response([_award(1)], has_next=False)]

    _source_run, _session, mock_client = _run_connector(responses=pages)

    call_url = mock_client.post.call_args.args[0]
    assert "spending_by_transaction" in call_url, (
        f"expected spending_by_transaction endpoint, got: {call_url}"
    )
    assert "spending_by_award" not in call_url, (
        "must not use the award endpoint — it returns null NAICS codes"
    )


# ─── Test 11: award type codes A/B/C/D present in request body ───────────────


def test_award_type_codes_sent_in_request_body():
    """
    Award type codes must cover contracts (A-D), grants (04/05), and IDVs —
    but not block/formula grants (02/03), loans (07/08), or direct payments
    (06,09,10,11). Fixed per John Cox Miller, Porter Capital, July 2026.
    """
    pages = [_mock_response([_award(1)], has_next=False)]

    _source_run, _session, mock_client = _run_connector(responses=pages)

    call_kwargs = mock_client.post.call_args
    sent_body = call_kwargs.kwargs.get("json") or call_kwargs.args[1]
    filters = sent_body.get("filters", {})
    codes = set(filters.get("award_type_codes", []))
    expected = {
        "A", "B", "C", "D",
        "04", "05",
        "IDV_A", "IDV_B", "IDV_B_A", "IDV_B_B", "IDV_B_C", "IDV_C", "IDV_D", "IDV_E",
    }
    assert codes == expected, f"award_type_codes must be exactly {expected}, got {codes}"
    for excluded in ("02", "03", "07", "08", "06", "09", "10", "11"):
        assert excluded not in codes, f"{excluded} must not be included (routes to non-B2B leads)"


# ─── Test 12: generated_internal_id requested in API fields ──────────────────


def test_invalid_fields_not_in_requested_fields():
    """
    'Action Type' and 'Action Type Description' are not valid field names for
    spending_by_transaction and cause HTTP 400 when included.  Guard against
    re-introduction.
    """
    from app.pipeline.connectors.usaspending import _FIELDS

    assert "Action Type" not in _FIELDS, (
        "'Action Type' is not a valid spending_by_transaction field — causes HTTP 400"
    )
    assert "Action Type Description" not in _FIELDS, (
        "'Action Type Description' is not a valid spending_by_transaction field — causes HTTP 400"
    )


def test_generated_internal_id_in_requested_fields():
    """
    The connector must request generated_internal_id from the API.
    Award ID is the PIID and cannot be used as a URL slug — it redirects
    to the USASpending homepage. generated_internal_id is the correct key.
    """
    from app.pipeline.connectors.usaspending import _FIELDS

    assert "generated_internal_id" in _FIELDS, (
        "generated_internal_id must be in _FIELDS so the API returns it"
    )


# ─── Test 13: source_url uses generated_internal_id, not bare Award ID ────────


def test_connector_source_url_uses_generated_internal_id():
    """
    source_url stored in raw_source_events must contain generated_internal_id
    (the USASpending URL slug), not just the PIID (Award ID).
    """
    award_data = _award(1)
    pages = [_mock_response([award_data], has_next=False)]

    _source_run, session, _client = _run_connector(responses=pages)

    added = session.add.call_args[0][0]
    assert award_data["generated_internal_id"] in added.source_url, (
        "source_url must embed generated_internal_id"
    )


# ─── Test 14: source_url is not the generic USASpending homepage ──────────────


def test_connector_source_url_is_not_homepage():
    """
    source_url must never be the generic USASpending homepage; it must be a
    deep link to a specific award.
    """
    pages = [_mock_response([_award(1)], has_next=False)]

    _source_run, session, _client = _run_connector(responses=pages)

    added = session.add.call_args[0][0]
    assert added.source_url is not None
    assert added.source_url.strip("/") != "https://www.usaspending.gov", (
        "source_url must not be the homepage"
    )
    assert "usaspending.gov/award/" in added.source_url, (
        "source_url must be an award detail URL"
    )


# ─── Retry / backoff helpers ──────────────────────────────────────────────────


def _retryable_response(status_code: int) -> MagicMock:
    """Mock response for a retryable HTTP error (429, 500, 502, 503, 504).

    status_code is set so the connector's retryable-set check fires.
    raise_for_status is never called on these mocks.
    """
    resp = MagicMock()
    resp.status_code = status_code
    return resp


def _permanent_error_response(status_code: int) -> MagicMock:
    """Mock response for a permanent client error (400, 401, 403, 404).

    status_code is NOT in the retryable set, so raise_for_status() is called
    and raises HTTPStatusError — connector re-raises immediately.
    """
    resp = MagicMock()
    resp.status_code = status_code
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        f"HTTP {status_code}", request=MagicMock(), response=resp
    )
    return resp


# ─── Test 15: HTTP 429 retries then succeeds ─────────────────────────────────


def test_http_429_retries_then_succeeds():
    """
    A 429 on the first attempt is retried; the second attempt succeeds.
    Exactly 2 HTTP calls are made and status='completed'.
    """
    responses = [
        _retryable_response(429),
        _mock_response([_award(1)], has_next=False),
    ]

    source_run, _session, mock_client = _run_connector(
        responses=responses, env={"USASPENDING_MAX_RETRIES": "3"}
    )

    assert mock_client.post.call_count == 2, "should retry once after 429"
    assert source_run.status == "completed"
    assert source_run.records_fetched == 1


# ─── Test 16: HTTP 500 retries then succeeds ─────────────────────────────────


def test_http_500_retries_then_succeeds():
    """
    A 500 on the first attempt is retried; the second attempt succeeds.
    Exactly 2 HTTP calls are made and status='completed'.
    """
    responses = [
        _retryable_response(500),
        _mock_response([_award(2)], has_next=False),
    ]

    source_run, _session, mock_client = _run_connector(
        responses=responses, env={"USASPENDING_MAX_RETRIES": "3"}
    )

    assert mock_client.post.call_count == 2, "should retry once after 500"
    assert source_run.status == "completed"
    assert source_run.records_fetched == 1


# ─── Test 17: HTTP 400 does not retry ────────────────────────────────────────


def test_http_400_does_not_retry():
    """
    A 400 is a permanent client error. The connector must not retry it —
    exactly 1 HTTP call is made and status='failed'.
    """
    responses = [_permanent_error_response(400)]

    source_run, _session, mock_client = _run_connector(responses=responses)

    assert mock_client.post.call_count == 1, "permanent 400 must not be retried"
    assert source_run.status == "failed"


# ─── Test 18: network timeout retries then succeeds ──────────────────────────


def test_network_timeout_retries_then_succeeds():
    """
    An httpx.TimeoutException on the first attempt is retried.
    The second attempt succeeds. status='completed'.
    """
    responses = [
        httpx.TimeoutException("read timed out"),
        _mock_response([_award(3)], has_next=False),
    ]

    source_run, _session, mock_client = _run_connector(
        responses=responses, env={"USASPENDING_MAX_RETRIES": "3"}
    )

    assert mock_client.post.call_count == 2, "should retry once after TimeoutException"
    assert source_run.status == "completed"
    assert source_run.records_fetched == 1


# ─── Test 19: connection error retries then succeeds ─────────────────────────


def test_connect_error_retries_then_succeeds():
    """
    An httpx.ConnectError (transient connection failure) on the first attempt
    is retried. The second attempt succeeds. status='completed'.
    """
    responses = [
        httpx.ConnectError("connection refused"),
        _mock_response([_award(4)], has_next=False),
    ]

    source_run, _session, mock_client = _run_connector(
        responses=responses, env={"USASPENDING_MAX_RETRIES": "3"}
    )

    assert mock_client.post.call_count == 2, "should retry once after ConnectError"
    assert source_run.status == "completed"
    assert source_run.records_fetched == 1


# ─── Test 20: retries exhausted → ConnectorError ─────────────────────────────


def test_retries_exhausted_raises_connector_error():
    """
    When all attempts fail (default max_retries=3 → 4 total attempts),
    ConnectorError is raised, status='failed', and error_text mentions USASpending.
    """
    responses = [_retryable_response(500)] * 4  # 1 initial + 3 retries

    source_run, _session, mock_client = _run_connector(
        responses=responses, env={"USASPENDING_MAX_RETRIES": "3"}
    )

    assert mock_client.post.call_count == 4, "should try exactly max_retries+1 times"
    assert source_run.status == "failed"
    assert source_run.error_text is not None
    assert "USASpending" in source_run.error_text


# ─── Test 21: USASPENDING_TIMEOUT_SECONDS passed to httpx.Client ─────────────


def test_timeout_env_var_passed_to_client():
    """
    When USASPENDING_TIMEOUT_SECONDS=45, httpx.Client must be constructed
    with timeout=45.0 — not the hardcoded 30.0 default.
    """
    pages = [_mock_response([_award(1)], has_next=False)]

    with patch("app.pipeline.connectors.usaspending.httpx.Client") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value.__enter__.return_value = mock_client
        mock_client.post.side_effect = pages

        with patch("app.pipeline.connectors.usaspending.time.sleep"):
            with patch(
                "app.pipeline.connectors.usaspending.random.uniform", return_value=0.0
            ):
                with patch.dict("os.environ", {
                    "USASPENDING_TIMEOUT_SECONDS": "45",
                    "USASPENDING_MAX_PAGES": "",
                    "USASPENDING_PAGE_LIMIT": "",
                    "USASPENDING_MAX_RETRIES": "",
                    "USASPENDING_BACKOFF_BASE_SECONDS": "",
                    "USASPENDING_BACKOFF_MAX_SECONDS": "",
                }):
                    connector = USASpendingConnector(
                        _make_session(), _make_source_run(), _make_source(),
                        fiscal_year=2025,
                    )
                    connector.run()

    assert mock_cls.call_args.kwargs["timeout"] == 45.0


# ─── Test 22: RemoteProtocolError retries then succeeds ──────────────────────


def test_remote_protocol_error_retries_then_succeeds():
    """
    An httpx.RemoteProtocolError (broken/incomplete HTTP response from server
    or proxy) on the first attempt is retried. The second attempt succeeds.
    """
    responses = [
        httpx.RemoteProtocolError("peer closed connection without sending complete message body"),
        _mock_response([_award(5)], has_next=False),
    ]

    source_run, _session, mock_client = _run_connector(
        responses=responses, env={"USASPENDING_MAX_RETRIES": "3"}
    )

    assert mock_client.post.call_count == 2, "should retry once after RemoteProtocolError"
    assert source_run.status == "completed"
    assert source_run.records_fetched == 1


# ─── Test 23: default max_pages is 200 when env var is absent ────────────────


def test_default_max_pages_is_200_when_env_var_absent():
    """
    When USASPENDING_MAX_PAGES is not set (or empty), max_pages must default
    to 200 — not None (unlimited). This cap prevents server-side disconnects
    on long-running pulls that fail around page 250.
    """
    with patch.dict(
        "os.environ",
        {
            "USASPENDING_MAX_PAGES": "",
            "USASPENDING_PAGE_LIMIT": "",
            "USASPENDING_TIMEOUT_SECONDS": "",
            "USASPENDING_MAX_RETRIES": "",
            "USASPENDING_BACKOFF_BASE_SECONDS": "",
            "USASPENDING_BACKOFF_MAX_SECONDS": "",
        },
    ):
        connector = USASpendingConnector(
            _make_session(), _make_source_run(), _make_source(), fiscal_year=2025
        )

    assert connector.max_pages == 200, (
        "max_pages must default to 200 when env var is absent — "
        "None (unlimited) is no longer the default"
    )


# ─── Test 24: env var override still works ────────────────────────────────────


def test_max_pages_env_var_override_respected():
    """
    When USASPENDING_MAX_PAGES=5 is set, the connector uses 5, not 200.
    Env var override must continue to work after the default changed from None.
    """
    with patch.dict(
        "os.environ",
        {
            "USASPENDING_MAX_PAGES": "5",
            "USASPENDING_PAGE_LIMIT": "",
            "USASPENDING_TIMEOUT_SECONDS": "",
            "USASPENDING_MAX_RETRIES": "",
            "USASPENDING_BACKOFF_BASE_SECONDS": "",
            "USASPENDING_BACKOFF_MAX_SECONDS": "",
        },
    ):
        connector = USASpendingConnector(
            _make_session(), _make_source_run(), _make_source(), fiscal_year=2025
        )
    assert connector.max_pages == 5, "explicit env var must override the 200 default"


def test_expanded_naics_sectors_in_connector() -> None:
    from app.pipeline.connectors.usaspending import _TARGET_NAICS_PREFIXES

    assert "21" in _TARGET_NAICS_PREFIXES
    assert "44" in _TARGET_NAICS_PREFIXES
    assert "45" in _TARGET_NAICS_PREFIXES
    assert "51" in _TARGET_NAICS_PREFIXES
    assert "55" in _TARGET_NAICS_PREFIXES
    assert "72" in _TARGET_NAICS_PREFIXES
    assert "81" in _TARGET_NAICS_PREFIXES


# ─── Test 25: $0/tiny amounts rejected, $10k+ accepted ────────────────────────


def test_amount_below_minimum_quarantined():
    """
    award_amount below $10,000 is administrative noise (de-obligations,
    corrections) and must be quarantined, not stored.
    """
    tiny = _award(1)
    tiny["Transaction Amount"] = 500.0
    pages = [_mock_response([tiny], has_next=False)]

    source_run, _session, _client = _run_connector(responses=pages)

    assert source_run.quarantine_count == 1
    assert source_run.records_valid == 0


def test_amount_at_minimum_accepted():
    """award_amount exactly at the $10,000 floor is accepted."""
    edge = _award(1)
    edge["Transaction Amount"] = 10_000.0
    pages = [_mock_response([edge], has_next=False)]

    source_run, _session, _client = _run_connector(responses=pages)

    assert source_run.quarantine_count == 0
    assert source_run.records_valid == 1


# ─── Test 26: activeness-based date filter ────────────────────────────────────


def test_old_award_excluded_even_with_pop_end_date_field_present():
    """
    spending_by_transaction (the endpoint this connector uses for NAICS data)
    has no period-of-performance-end-date field under any name — confirmed
    live 2026-07-07 against the real API, which rejects it with HTTP 400. A
    "Period of Performance Current End Date" key in the raw payload is
    therefore just ignored by USASpendingRecord (extra fields are dropped),
    and activeness is lookback-only: an old action_date is excluded even if
    the raw payload happens to carry a future end-date-shaped key.
    """
    old_but_would_have_been_active = _award(1)
    old_but_would_have_been_active["Action Date"] = "2019-01-01"
    old_but_would_have_been_active["Period of Performance Current End Date"] = "2099-01-01"
    pages = [_mock_response([old_but_would_have_been_active], has_next=False)]

    source_run, _session, _client = _run_connector(responses=pages)

    assert source_run.records_valid == 0
    assert source_run.records_skipped == 1


def test_expired_and_old_award_excluded():
    """
    An award with an action_date older than the lookback window must be
    skipped (records_skipped, not quarantined — it passed validation, it's
    just not recent).
    """
    stale = _award(1)
    stale["Action Date"] = "2015-01-01"
    pages = [_mock_response([stale], has_next=False)]

    source_run, _session, _client = _run_connector(responses=pages)

    assert source_run.records_valid == 0
    assert source_run.records_skipped == 1


def test_recent_award_included():
    """An award within the lookback window is included."""
    recent = _award(1)  # Action Date 2025-03-15
    pages = [_mock_response([recent], has_next=False)]

    source_run, _session, _client = _run_connector(responses=pages)

    assert source_run.records_valid == 1


# ─── Test 27: multi-year backfill ─────────────────────────────────────────────


def test_multiyear_backfill_when_fiscal_year_not_pinned():
    """
    When fiscal_year is not explicitly passed to the connector, it fetches
    USASPENDING_LOOKBACK_YEARS fiscal years (default 3), issuing one request
    per fiscal year.
    """
    pages = [
        _mock_response([], has_next=False),
        _mock_response([], has_next=False),
        _mock_response([], has_next=False),
    ]
    session = _make_session()
    source = _make_source()
    source_run = _make_source_run()

    env_patch = {
        "USASPENDING_MAX_PAGES": "",
        "USASPENDING_PAGE_LIMIT": "",
        "USASPENDING_TIMEOUT_SECONDS": "",
        "USASPENDING_MAX_RETRIES": "",
        "USASPENDING_BACKOFF_BASE_SECONDS": "",
        "USASPENDING_BACKOFF_MAX_SECONDS": "",
        "USASPENDING_LOOKBACK_YEARS": "3",
    }

    with patch("app.pipeline.connectors.usaspending.httpx.Client") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value.__enter__.return_value = mock_client
        mock_client.post.side_effect = pages

        with patch("app.pipeline.connectors.usaspending.time.sleep"):
            with patch("app.pipeline.connectors.usaspending.random.uniform", return_value=0.0):
                with patch.dict("os.environ", env_patch):
                    connector = USASpendingConnector(session, source_run, source)
                    connector.run()

    assert mock_client.post.call_count == 3, "one request per lookback year"
    assert source_run.status == "completed"


def test_explicit_fiscal_year_stays_single_year():
    """
    Passing fiscal_year explicitly (as tests and manual backfills do) must
    NOT trigger multi-year backfill — exactly one request is made.
    """
    pages = [_mock_response([_award(1)], has_next=False)]

    _source_run, _session, mock_client = _run_connector(responses=pages)

    assert mock_client.post.call_count == 1, "explicit fiscal_year must stay single-year"


# ─── Test 28: award type classification ───────────────────────────────────────


def test_classify_award_type_contract():
    from app.pipeline.connectors.usaspending import classify_award_type

    assert classify_award_type(None) == "CONTRACT_AWARD"
    assert classify_award_type("DEFINITIVE CONTRACT") == "CONTRACT_AWARD"


def test_classify_award_type_idv():
    from app.pipeline.connectors.usaspending import classify_award_type

    assert classify_award_type("IDV_B_A") == "IDV_AWARD"
    assert classify_award_type("IDV") == "IDV_AWARD"


def test_classify_award_type_grant():
    from app.pipeline.connectors.usaspending import classify_award_type

    assert classify_award_type("04") == "FEDERAL_GRANT"
    assert classify_award_type("COOPERATIVE AGREEMENT") == "FEDERAL_GRANT"
