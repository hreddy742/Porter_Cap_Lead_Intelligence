"""
Tests for app/enrichment/providers/sam_gov.py (live SAM.gov provider).

No real network calls — httpx.Client is replaced with a fake that yields
queued responses or raises queued exceptions. time.sleep is patched to no-op
so retry backoff does not slow the suite.

Tests:
  1.  matched response → match_status='matched', registration_status, address
  2.  not_found response (empty entityData)
  3.  no UEI → 'no_uei' with no HTTP call
  4.  429 then 200 → retries then succeeds
  5.  5xx every attempt → retries then 'error'
  6.  timeout every attempt → 'error'
  7.  401 → 'error', no retry (single call)
  8.  403 → 'error', no retry (single call)
  9.  dry_run → no HTTP call
  10. missing SAM_GOV_API_KEY → 'error', no HTTP call
  11. api_key is never written to logs
"""
from __future__ import annotations

import httpx
import pytest

import app.enrichment.providers.sam_gov as sam_gov
from app.enrichment.providers.sam_gov import SAMGovProvider

_UEI = "ABC123DEF456"

_MATCHED_PAYLOAD = {
    "totalRecords": 1,
    "entityData": [
        {
            "entityRegistration": {
                "ueiSAM": _UEI,
                "legalBusinessName": "Acme Federal Services LLC",
                "registrationStatus": "Active",
            },
            "coreData": {
                "physicalAddress": {
                    "addressLine1": "100 Main St",
                    "city": "Austin",
                    "stateOrProvinceCode": "TX",
                    "zipCode": "78701",
                    "countryCode": "USA",
                }
            },
        }
    ],
}

_NOT_FOUND_PAYLOAD = {"totalRecords": 0, "entityData": []}


class _FakeResponse:
    def __init__(self, status_code: int, json_data: dict | None = None):
        self.status_code = status_code
        self._json = json_data or {}

    def json(self) -> dict:
        return self._json


class _FakeClient:
    """Context-manager client that returns/raises queued items per get() call."""

    def __init__(self, items: list):
        self._items = list(items)
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, params=None, headers=None):
        self.calls += 1
        item = self._items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def patched(monkeypatch):
    """Patch time.sleep to no-op and return a helper to install a fake client."""
    monkeypatch.setattr(sam_gov.time, "sleep", lambda _s: None)
    monkeypatch.setenv("SAM_GOV_API_KEY", "test-key-do-not-log")

    state: dict = {"client": None}

    def install(items: list) -> _FakeClient:
        client = _FakeClient(items)
        state["client"] = client
        monkeypatch.setattr(sam_gov.httpx, "Client", lambda **_kw: client)
        return client

    state["install"] = install
    return state


# ── 1: matched ────────────────────────────────────────────────────────────────

def test_matched_response(patched):
    client = patched["install"]([_FakeResponse(200, _MATCHED_PAYLOAD)])

    result = SAMGovProvider().lookup_entity(_UEI)

    assert result.match_status == "matched"
    assert result.uei == _UEI
    assert result.registration_status == "Active"
    assert result.address == "100 Main St, Austin, TX, 78701, USA"
    assert client.calls == 1


# ── 2: not_found ────────────────────────────────────────────────────────────────

def test_not_found_response(patched):
    patched["install"]([_FakeResponse(200, _NOT_FOUND_PAYLOAD)])

    result = SAMGovProvider().lookup_entity(_UEI)

    assert result.match_status == "not_found"
    assert result.registration_status is None
    assert result.address is None


# ── 3: no UEI, no HTTP ──────────────────────────────────────────────────────────

def test_no_uei_returns_no_uei_without_http(monkeypatch):
    called = {"n": 0}

    def _boom(**_kw):
        called["n"] += 1
        raise AssertionError("httpx.Client must not be constructed when UEI is missing")

    monkeypatch.setattr(sam_gov.httpx, "Client", _boom)

    result = SAMGovProvider().lookup_entity(None)

    assert result.match_status == "no_uei"
    assert result.uei is None
    assert called["n"] == 0


def test_blank_uei_returns_no_uei(monkeypatch):
    monkeypatch.setattr(
        sam_gov.httpx, "Client",
        lambda **_kw: (_ for _ in ()).throw(AssertionError("no HTTP expected")),
    )
    result = SAMGovProvider().lookup_entity("   ")
    assert result.match_status == "no_uei"


# ── 4: 429 then success ─────────────────────────────────────────────────────────

def test_429_retries_then_succeeds(patched):
    client = patched["install"](
        [_FakeResponse(429), _FakeResponse(200, _MATCHED_PAYLOAD)]
    )

    result = SAMGovProvider().lookup_entity(_UEI)

    assert result.match_status == "matched"
    assert client.calls == 2


# ── 5: 5xx exhausts retries then errors ─────────────────────────────────────────

def test_5xx_retries_then_fails_safely(patched):
    client = patched["install"]([_FakeResponse(503) for _ in range(4)])

    result = SAMGovProvider(max_retries=3).lookup_entity(_UEI)

    assert result.match_status == "error"
    assert result.uei == _UEI
    assert client.calls == 4  # initial + 3 retries


# ── 6: timeout returns error ────────────────────────────────────────────────────

def test_timeout_returns_error(patched):
    client = patched["install"](
        [httpx.TimeoutException("timed out") for _ in range(4)]
    )

    result = SAMGovProvider(max_retries=3).lookup_entity(_UEI)

    assert result.match_status == "error"
    assert client.calls == 4


# ── 7 & 8: 401/403 return error without retry ───────────────────────────────────

def test_401_returns_error_no_retry(patched):
    client = patched["install"]([_FakeResponse(401)])

    result = SAMGovProvider().lookup_entity(_UEI)

    assert result.match_status == "error"
    assert client.calls == 1  # no retry


def test_403_returns_error_no_retry(patched):
    client = patched["install"]([_FakeResponse(403)])

    result = SAMGovProvider().lookup_entity(_UEI)

    assert result.match_status == "error"
    assert client.calls == 1


# ── 9: dry_run makes no HTTP call ───────────────────────────────────────────────

def test_dry_run_makes_no_http_call(monkeypatch):
    monkeypatch.setenv("SAM_GOV_API_KEY", "test-key")
    monkeypatch.setattr(
        sam_gov.httpx, "Client",
        lambda **_kw: (_ for _ in ()).throw(AssertionError("no HTTP in dry_run")),
    )

    result = SAMGovProvider().lookup_entity(_UEI, dry_run=True)

    assert result.match_status == "error"  # no live lookup performed


# ── 10: missing API key handled safely, no HTTP ─────────────────────────────────

def test_missing_api_key_returns_error_no_http(monkeypatch):
    monkeypatch.delenv("SAM_GOV_API_KEY", raising=False)
    monkeypatch.setattr(
        sam_gov.httpx, "Client",
        lambda **_kw: (_ for _ in ()).throw(AssertionError("no HTTP without API key")),
    )

    result = SAMGovProvider().lookup_entity(_UEI)

    assert result.match_status == "error"
    assert result.uei == _UEI


# ── 11: api_key is never logged ─────────────────────────────────────────────────

def test_api_key_not_logged(patched, capsys):
    # The fixture sets SAM_GOV_API_KEY="test-key-do-not-log".
    patched["install"]([_FakeResponse(200, _MATCHED_PAYLOAD)])

    SAMGovProvider().lookup_entity(_UEI)

    captured = capsys.readouterr()
    assert "test-key-do-not-log" not in captured.out
    assert "test-key-do-not-log" not in captured.err
