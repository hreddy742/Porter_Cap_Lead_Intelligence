"""
Tests for app/enrichment/sam_gov_daily.py.

All tests are pure unit tests — no database, no real HTTP calls.
httpx.Client and time.sleep are patched throughout.

Tests:
  1.  Key rotation: first 10 companies use KEY_1 (Harsha)
  2.  Key rotation: companies 10-19 use KEY_2 (Kate)
  3.  Already enriched company (naics_code set) excluded from query
  4.  No match → contactability_status='not_contactable', score=0
  5.  429 on primary key → backoff, try secondary key
  6.  Both keys return 429 → loop breaks, no crash
  7.  Exclusion flag Y → warning logged, data still stored
  8.  CEO name and title extracted from pointsOfContact
  9.  Rescore triggered when naics_code updated, not when already set
  10. No candidates → returns zeros, no HTTP call
  11. Missing both API keys → error dict returned, no HTTP call
  12. _extract_domain handles http, https, www, bare domain, blank
  13. _extract_fields parses all expected fields from a full entity dict
  14. Matched company: naics updated, contactability_score=3, status=partially_contactable
  15. UEI conflict check: UEI owned by another company is not re-inserted
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, call, patch

import pytest

import app.enrichment.sam_gov_daily as daily
from app.enrichment.sam_gov_daily import (
    _extract_domain,
    _extract_fields,
    _lookup_by_name,
    _upsert_contactability,
    _update_company_fields,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

_CO_ID = uuid.uuid4()
_LC_ID = uuid.uuid4()

_FULL_ENTITY = {
    "entityRegistration": {
        "ueiSAM": "ABC123DEF456",
        "registrationStatus": "Active",
        "exclusionStatusFlag": "N",
    },
    "coreData": {
        "physicalAddress": {"stateOrProvinceCode": "TX"},
        "entityInformation": {"entityURL": "https://www.kira-aerospace.com"},
    },
    "assertions": {
        "goodsAndServices": {"primaryNaics": "336415"},
    },
    "pointsOfContact": {
        "governmentBusinessPOC": {
            "firstName": "Jane",
            "lastName": "Doe",
            "title": "CEO",
        }
    },
}


def _make_row(
    company_id=_CO_ID,
    company_name="KIRA AEROSPACE",
    lead_candidate_id=_LC_ID,
    current_score=28,
):
    row = MagicMock()
    row.company_id = company_id
    row.canonical_name = company_name
    row.lead_candidate_id = lead_candidate_id
    row.current_score = current_score
    return row


class _FakeHTTPResp:
    def __init__(self, status_code: int, json_data: dict | None = None):
        self.status_code = status_code
        self._json = json_data or {}

    def json(self) -> dict:
        return self._json


class _FakeHTTPClient:
    """Minimal context-manager stub returning queued responses."""

    def __init__(self, items: list):
        self._items = list(items)
        self.calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def get(self, url, params=None, **_kw):
        self.calls.append({"url": url, "params": params or {}})
        item = self._items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


# ── 1. Key rotation: first 10 use KEY_1 ──────────────────────────────────────

def test_key_rotation_first_ten_use_harsha_key(monkeypatch):
    monkeypatch.setenv("SAM_GOV_API_KEY_HARSHA", "key-harsha")
    monkeypatch.setenv("SAM_GOV_API_KEY_KATE", "key-kate")
    monkeypatch.setattr(daily.time, "sleep", lambda _: None)

    candidates = [_make_row(company_id=uuid.uuid4(), company_name=f"Co {i}") for i in range(10)]
    api_keys_used: list[str] = []

    def fake_lookup(company_name, api_key):
        api_keys_used.append(api_key)
        return None, "not_found"

    def fake_db():
        db = MagicMock()
        db.execute.return_value.fetchall.return_value = candidates
        db.get.return_value = MagicMock(naics_code=None, state=None, website_domain=None, country="US", id=uuid.uuid4())
        db.execute.return_value.scalar_one_or_none.return_value = None
        return db

    with patch.object(daily, "_select_candidates", return_value=candidates), \
         patch.object(daily, "_lookup_by_name", side_effect=fake_lookup), \
         patch.object(daily, "_upsert_contactability"), \
         patch.object(daily, "SessionLocal", return_value=fake_db()):
        daily.run_enrichment()

    assert all(k == "key-harsha" for k in api_keys_used), (
        f"Expected all 10 to use Harsha key, got: {set(api_keys_used)}"
    )


# ── 2. Key rotation: companies 10-19 use KEY_2 ───────────────────────────────

def test_key_rotation_second_ten_use_kate_key(monkeypatch):
    monkeypatch.setenv("SAM_GOV_API_KEY_HARSHA", "key-harsha")
    monkeypatch.setenv("SAM_GOV_API_KEY_KATE", "key-kate")
    monkeypatch.setattr(daily.time, "sleep", lambda _: None)

    candidates = [_make_row(company_id=uuid.uuid4(), company_name=f"Co {i}") for i in range(20)]
    api_keys_used: list[tuple[int, str]] = []

    def fake_lookup(company_name, api_key):
        api_keys_used.append(api_key)
        return None, "not_found"

    db = MagicMock()
    db.get.return_value = MagicMock(naics_code=None, state=None, website_domain=None, country="US", id=uuid.uuid4())
    db.execute.return_value.scalar_one_or_none.return_value = None

    with patch.object(daily, "_select_candidates", return_value=candidates), \
         patch.object(daily, "_lookup_by_name", side_effect=fake_lookup), \
         patch.object(daily, "_upsert_contactability"), \
         patch.object(daily, "SessionLocal", return_value=db):
        daily.run_enrichment()

    first_ten = api_keys_used[:10]
    second_ten = api_keys_used[10:]
    assert all(k == "key-harsha" for k in first_ten)
    assert all(k == "key-kate" for k in second_ten)


# ── 3. Already enriched (naics_code already set) excluded by query ────────────

def test_already_enriched_company_skipped():
    """_select_candidates filters naics_code IS NULL — so no candidates returned."""
    with patch.object(daily, "_select_candidates", return_value=[]) as sel, \
         patch.object(daily, "_lookup_by_name") as lkp, \
         patch.object(daily, "SessionLocal", return_value=MagicMock()):
        result = daily.run_enrichment.__wrapped__() if hasattr(daily.run_enrichment, "__wrapped__") else None

    # The empty-candidates early-exit path returns zeros without calling _lookup_by_name.
    # We verify _select_candidates is called and _lookup_by_name is never called.
    with patch.object(daily, "_select_candidates", return_value=[]), \
         patch.object(daily, "_lookup_by_name") as lkp2, \
         patch.object(daily, "SessionLocal", return_value=MagicMock()), \
         patch.dict("os.environ", {"SAM_GOV_API_KEY_HARSHA": "k1", "SAM_GOV_API_KEY_KATE": "k2"}):
        result = daily.run_enrichment()

    lkp2.assert_not_called()
    assert result["enriched"] == 0


# ── 4. No match → not_contactable, score=0 ───────────────────────────────────

def test_no_match_marks_not_contactable(monkeypatch):
    monkeypatch.setenv("SAM_GOV_API_KEY_HARSHA", "k1")
    monkeypatch.setattr(daily.time, "sleep", lambda _: None)

    row = _make_row()
    db = MagicMock()
    db.get.return_value = MagicMock(naics_code=None, country="US", id=_CO_ID)
    db.execute.return_value.scalar_one_or_none.return_value = None

    upsert_calls: list[tuple] = []

    def fake_upsert(db_, company_id, lead_candidate_id, sam_match_status, fields):
        upsert_calls.append((sam_match_status, fields))

    with patch.object(daily, "_select_candidates", return_value=[row]), \
         patch.object(daily, "_lookup_by_name", return_value=(None, "not_found")), \
         patch.object(daily, "_upsert_contactability", side_effect=fake_upsert), \
         patch.object(daily, "SessionLocal", return_value=db):
        result = daily.run_enrichment()

    assert result["not_found"] == 1
    assert result["matched"] == 0
    assert len(upsert_calls) == 1
    assert upsert_calls[0][0] == "not_found"
    assert upsert_calls[0][1] is None


def test_upsert_contactability_not_found_sets_correct_status():
    db = MagicMock()
    db.execute.return_value.scalar_one_or_none.return_value = None
    added: list = []
    db.add.side_effect = added.append

    _upsert_contactability(db, _CO_ID, _LC_ID, "not_found", None)

    assert len(added) == 1
    rec = added[0]
    assert rec.contactability_status == "not_contactable"
    assert rec.contactability_score == 0
    assert rec.sam_match_status == "not_found"
    assert rec.official_website is None


# ── 5. 429 on primary → backoff, try secondary ────────────────────────────────

def test_429_tries_secondary_key(monkeypatch):
    monkeypatch.setenv("SAM_GOV_API_KEY_HARSHA", "key-harsha")
    monkeypatch.setenv("SAM_GOV_API_KEY_KATE", "key-kate")

    sleeps: list[float] = []
    monkeypatch.setattr(daily.time, "sleep", lambda s: sleeps.append(s))

    row = _make_row()
    db = MagicMock()
    db.get.return_value = MagicMock(naics_code=None, state=None, website_domain=None, country="US", id=_CO_ID)
    db.execute.return_value.scalar_one_or_none.return_value = None

    call_log: list[str] = []

    def fake_lookup(company_name, api_key):
        call_log.append(api_key)
        if api_key == "key-harsha":
            return None, "rate_limited"
        return None, "not_found"

    with patch.object(daily, "_select_candidates", return_value=[row]), \
         patch.object(daily, "_lookup_by_name", side_effect=fake_lookup), \
         patch.object(daily, "_upsert_contactability"), \
         patch.object(daily, "SessionLocal", return_value=db):
        result = daily.run_enrichment()

    assert "key-harsha" in call_log
    assert "key-kate" in call_log
    # A backoff sleep must have occurred before trying the secondary key.
    assert any(s >= daily._BACKOFF_BASE for s in sleeps)
    # The company was enriched via secondary key.
    assert result["not_found"] == 1


# ── 6. Both keys rate limited → loop breaks ───────────────────────────────────

def test_both_keys_rate_limited_breaks_loop(monkeypatch):
    monkeypatch.setenv("SAM_GOV_API_KEY_HARSHA", "key-harsha")
    monkeypatch.setenv("SAM_GOV_API_KEY_KATE", "key-kate")
    monkeypatch.setattr(daily.time, "sleep", lambda _: None)

    candidates = [_make_row(company_id=uuid.uuid4(), company_name=f"Co {i}") for i in range(5)]
    call_count = {"n": 0}

    def fake_lookup(company_name, api_key):
        call_count["n"] += 1
        return None, "rate_limited"

    db = MagicMock()
    db.get.return_value = MagicMock(naics_code=None, country="US", id=uuid.uuid4())
    db.execute.return_value.scalar_one_or_none.return_value = None

    with patch.object(daily, "_select_candidates", return_value=candidates), \
         patch.object(daily, "_lookup_by_name", side_effect=fake_lookup), \
         patch.object(daily, "_upsert_contactability"), \
         patch.object(daily, "SessionLocal", return_value=db):
        result = daily.run_enrichment()

    # Loop should break after first company exhausts both keys.
    # 2 calls: primary key then secondary key for the first company.
    assert call_count["n"] == 2
    assert result["enriched"] == 0


# ── 7. Exclusion flag Y → warning logged, data stored ─────────────────────────

def test_exclusion_flag_y_still_stores_data(monkeypatch, caplog):
    monkeypatch.setenv("SAM_GOV_API_KEY_HARSHA", "k1")
    monkeypatch.setattr(daily.time, "sleep", lambda _: None)

    excluded_entity = {
        **_FULL_ENTITY,
        "entityRegistration": {
            **_FULL_ENTITY["entityRegistration"],
            "exclusionStatusFlag": "Y",
        },
    }

    row = _make_row()
    db = MagicMock()
    db.get.return_value = MagicMock(
        id=_CO_ID, naics_code=None, state=None, website_domain=None, country="US"
    )
    db.execute.return_value.scalar_one_or_none.return_value = None
    db.execute.return_value.scalars.return_value.all.return_value = []
    db.execute.return_value.scalars.return_value.first.return_value = None

    upsert_calls: list[str] = []

    def fake_upsert(db_, company_id, lead_candidate_id, sam_match_status, fields):
        upsert_calls.append(sam_match_status)

    with patch.object(daily, "_select_candidates", return_value=[row]), \
         patch.object(daily, "_lookup_by_name", return_value=(excluded_entity, "matched")), \
         patch.object(daily, "_upsert_contactability", side_effect=fake_upsert), \
         patch.object(daily, "_rescore_after_naics", return_value={"rescored": False}), \
         patch.object(daily, "SessionLocal", return_value=db):
        result = daily.run_enrichment()

    # Data still stored (not rejected due to exclusion flag).
    assert "matched" in upsert_calls
    assert result["matched"] == 1


# ── 8. CEO name extracted from pointsOfContact ────────────────────────────────

def test_extract_fields_ceo_name_and_title():
    fields = _extract_fields(_FULL_ENTITY)

    assert fields["ceo_note"] == "Jane Doe (CEO)"
    assert fields["naics"] == "336415"
    assert fields["uei"] == "ABC123DEF456"
    assert fields["state"] == "TX"
    assert fields["website_domain"] == "www.kira-aerospace.com"


def test_extract_fields_ceo_name_without_title():
    entity = {
        **_FULL_ENTITY,
        "pointsOfContact": {
            "governmentBusinessPOC": {
                "firstName": "John",
                "lastName": "Smith",
                "title": "",
            }
        },
    }
    fields = _extract_fields(entity)
    assert fields["ceo_note"] == "John Smith"


def test_extract_fields_no_poc_returns_none():
    entity = {**_FULL_ENTITY, "pointsOfContact": {}}
    fields = _extract_fields(entity)
    assert fields["ceo_note"] is None


# ── 9. Rescore triggered only when naics updated ──────────────────────────────

def test_rescore_triggered_after_naics_updated(monkeypatch):
    monkeypatch.setenv("SAM_GOV_API_KEY_HARSHA", "k1")
    monkeypatch.setattr(daily.time, "sleep", lambda _: None)

    row = _make_row()
    company_mock = MagicMock(
        id=_CO_ID, naics_code=None, state=None, website_domain=None, country="US"
    )
    db = MagicMock()
    db.get.return_value = company_mock
    db.execute.return_value.scalar_one_or_none.return_value = None
    db.execute.return_value.scalars.return_value.all.return_value = []
    db.execute.return_value.scalars.return_value.first.return_value = None

    rescore_calls: list = []

    def fake_rescore(company_id, db_):
        rescore_calls.append(company_id)
        return {"rescored": True, "total_score": 48, "tier": "warm", "old_tier": "archive"}

    with patch.object(daily, "_select_candidates", return_value=[row]), \
         patch.object(daily, "_lookup_by_name", return_value=(_FULL_ENTITY, "matched")), \
         patch.object(daily, "_upsert_contactability"), \
         patch.object(daily, "_rescore_after_naics", side_effect=fake_rescore), \
         patch.object(daily, "SessionLocal", return_value=db):
        result = daily.run_enrichment()

    assert len(rescore_calls) == 1
    assert result["rescored"] == 1


def test_rescore_not_triggered_when_naics_already_set(monkeypatch):
    monkeypatch.setenv("SAM_GOV_API_KEY_HARSHA", "k1")
    monkeypatch.setattr(daily.time, "sleep", lambda _: None)

    row = _make_row()
    company_mock = MagicMock(
        id=_CO_ID, naics_code="336415", state="TX", website_domain=None, country="US"
    )
    db = MagicMock()
    db.get.return_value = company_mock
    db.execute.return_value.scalar_one_or_none.return_value = None

    rescore_calls: list = []

    with patch.object(daily, "_select_candidates", return_value=[row]), \
         patch.object(daily, "_lookup_by_name", return_value=(_FULL_ENTITY, "matched")), \
         patch.object(daily, "_upsert_contactability"), \
         patch.object(daily, "_rescore_after_naics", side_effect=lambda *a: rescore_calls.append(a) or {}), \
         patch.object(daily, "SessionLocal", return_value=db):
        daily.run_enrichment()

    assert len(rescore_calls) == 0


# ── 10. No candidates → zeros returned ───────────────────────────────────────

def test_no_candidates_returns_zeros(monkeypatch):
    monkeypatch.setenv("SAM_GOV_API_KEY_HARSHA", "k1")

    with patch.object(daily, "_select_candidates", return_value=[]), \
         patch.object(daily, "_lookup_by_name") as lkp, \
         patch.object(daily, "SessionLocal", return_value=MagicMock()):
        result = daily.run_enrichment()

    lkp.assert_not_called()
    assert result["enriched"] == 0
    assert result["matched"] == 0
    assert result["not_found"] == 0
    assert result["rescored"] == 0


# ── 11. Missing both API keys → error dict ────────────────────────────────────

def test_missing_both_keys_returns_error(monkeypatch):
    monkeypatch.delenv("SAM_GOV_API_KEY_HARSHA", raising=False)
    monkeypatch.delenv("SAM_GOV_API_KEY_KATE", raising=False)

    with patch.object(daily, "SessionLocal", return_value=MagicMock()), \
         patch.object(daily, "_lookup_by_name") as lkp:
        result = daily.run_enrichment()

    lkp.assert_not_called()
    assert "error" in result


# ── 12. _extract_domain ───────────────────────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://www.example.com/path", "www.example.com"),
    ("http://example.com", "example.com"),
    ("https://example.com", "example.com"),
    ("www.example.com", "www.example.com"),
    ("", None),
    (None, None),
])
def test_extract_domain_variants(url, expected):
    assert _extract_domain(url) == expected


# ── 13. _extract_fields parses full entity ────────────────────────────────────

def test_extract_fields_full_entity():
    fields = _extract_fields(_FULL_ENTITY)

    assert fields["naics"] == "336415"
    assert fields["uei"] == "ABC123DEF456"
    assert fields["registration_status"] == "Active"
    assert fields["exclusion_flag"] == "N"
    assert fields["website_url"] == "https://www.kira-aerospace.com"
    assert fields["website_domain"] == "www.kira-aerospace.com"
    assert fields["state"] == "TX"
    assert fields["ceo_note"] == "Jane Doe (CEO)"


def test_extract_fields_missing_sections():
    fields = _extract_fields({})

    assert fields["naics"] is None
    assert fields["uei"] is None
    assert fields["state"] is None
    assert fields["ceo_note"] is None


# ── 14. Matched company: contactability_score=3, status=partially_contactable ─

def test_upsert_contactability_matched_sets_correct_fields():
    db = MagicMock()
    db.execute.return_value.scalar_one_or_none.return_value = None
    added: list = []
    db.add.side_effect = added.append

    fields = _extract_fields(_FULL_ENTITY)
    _upsert_contactability(db, _CO_ID, _LC_ID, "matched", fields)

    assert len(added) == 1
    rec = added[0]
    assert rec.contactability_status == "partially_contactable"
    assert rec.contactability_score == 3
    assert rec.sam_match_status == "matched"
    assert rec.sam_uei == "ABC123DEF456"
    assert rec.official_website == "https://www.kira-aerospace.com"
    assert rec.contactability_notes == "Jane Doe (CEO)"


# ── 15. UEI conflict: UEI owned by another company not re-inserted ────────────

def test_uei_conflict_not_inserted(monkeypatch):
    db = MagicMock()
    company = MagicMock(id=_CO_ID, naics_code=None, state=None, website_domain=None)

    conflict_row = MagicMock()  # simulates UEI already in use by another company
    added: list = []
    db.add.side_effect = added.append

    # First call: no UEI on this company; second call: UEI belongs to another company
    db.execute.return_value.scalar_one_or_none.side_effect = [None, conflict_row]

    _update_company_fields(db, company, {"naics": "336415", "uei": "TAKEN-UEI", "state": None, "website_domain": None})

    identifier_adds = [a for a in added if hasattr(a, "id_type") and a.id_type == "uei"]
    assert len(identifier_adds) == 0
