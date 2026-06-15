"""
Tests for app/enrichment/orchestrator.py

All tests use MagicMock DB sessions.
No real DB, no network calls.

Tests:
  1.  dry_run=True returns correct summary, no DB writes
  2.  apply=False forces dry_run regardless of dry_run parameter
  3.  dry_run returns companies_would_enrich count from DB query
  4.  company_id mode queries single company
  5.  dry_run makes no db.add() or db.commit() calls
  6.  apply mode with empty company list returns zero enriched
  7.  providers are not called in dry_run mode (no HTTP calls)
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest

from app.db.models import CompanyContactability
from app.enrichment.orchestrator import run_contactability_enrichment
from app.enrichment.providers.base import SAMResult, WebsiteContactResult, WebsiteResult


def _make_company_row(**kwargs):
    defaults = {
        "company_id": uuid.uuid4(),
        "canonical_name": "Acme Federal Services LLC",
        "state": "TX",
        "uei": "ABC123DEF456",
        "lead_candidate_id": uuid.uuid4(),
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _make_mock_db(rows: list):
    """Return a MagicMock DB whose execute().fetchall() returns rows."""
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = rows
    return db


# ── Test 1: dry_run returns correct summary ───────────────────────────────────

def test_dry_run_returns_summary():
    rows = [_make_company_row(), _make_company_row()]
    db = _make_mock_db(rows)

    result = run_contactability_enrichment(db=db, limit=25, dry_run=True, apply=False)

    assert result["dry_run"] is True
    assert result["companies_would_enrich"] == 2
    assert result["limit"] == 25


# ── Test 2: apply=False forces dry_run ────────────────────────────────────────

def test_apply_false_forces_dry_run():
    rows = [_make_company_row()]
    db = _make_mock_db(rows)

    result = run_contactability_enrichment(db=db, limit=10, dry_run=False, apply=False)

    assert result["dry_run"] is True


# ── Test 3: dry_run no DB writes ──────────────────────────────────────────────

def test_dry_run_no_db_writes():
    rows = [_make_company_row(), _make_company_row(), _make_company_row()]
    db = _make_mock_db(rows)

    run_contactability_enrichment(db=db, limit=25, dry_run=True, apply=False)

    db.add.assert_not_called()
    db.commit.assert_not_called()


# ── Test 4: dry_run companies_would_enrich reflects query result ──────────────

def test_dry_run_companies_would_enrich_count():
    rows = [_make_company_row() for _ in range(7)]
    db = _make_mock_db(rows)

    result = run_contactability_enrichment(db=db, limit=50, dry_run=True, apply=False)

    assert result["companies_would_enrich"] == 7


# ── Test 5: dry_run with zero results ────────────────────────────────────────

def test_dry_run_zero_companies():
    db = _make_mock_db([])

    result = run_contactability_enrichment(db=db, limit=25, dry_run=True, apply=False)

    assert result["dry_run"] is True
    assert result["companies_would_enrich"] == 0
    db.add.assert_not_called()
    db.commit.assert_not_called()


# ── Test 6: providers not called in dry_run ───────────────────────────────────

def test_dry_run_providers_not_called():
    rows = [_make_company_row()]
    db = _make_mock_db(rows)

    mock_search = MagicMock()
    mock_sam = MagicMock()

    run_contactability_enrichment(
        db=db,
        limit=25,
        dry_run=True,
        apply=False,
        search_provider=mock_search,
        sam_provider=mock_sam,
    )

    mock_search.find_company_website.assert_not_called()
    mock_sam.lookup_entity.assert_not_called()


# ── Test 7: apply mode with stub providers returns apply summary ──────────────

def test_apply_mode_stub_providers_enriches(monkeypatch):
    rows = [_make_company_row()]
    db = MagicMock()

    # fetchall for _select_companies
    db.execute.return_value.fetchall.return_value = rows
    # scalar_one_or_none for existing CC check in _enrich_one
    db.execute.return_value.scalar_one_or_none.return_value = None

    mock_search = MagicMock()
    mock_search.find_company_website.return_value = None

    mock_sam = MagicMock()
    mock_sam.lookup_entity.return_value = SAMResult(uei=None, match_status="no_uei")

    result = run_contactability_enrichment(
        db=db,
        limit=1,
        dry_run=False,
        apply=True,
        search_provider=mock_search,
        sam_provider=mock_sam,
    )

    assert result["dry_run"] is False
    assert result["companies_attempted"] == 1
    assert result["companies_enriched"] == 1
    assert result["companies_failed"] == 0
    db.add.assert_called()
    db.commit.assert_called()


# ── Test 8: tier and source are returned in dry_run summary ───────────────────

def test_dry_run_summary_includes_tier_and_source():
    db = _make_mock_db([])

    result = run_contactability_enrichment(
        db=db, limit=10, tier="cold", source="sam", dry_run=True, apply=False
    )

    assert result["tier"] == "cold"
    assert result["source"] == "sam"


# ── Test 9: company_id mode uses fetchone not fetchall ────────────────────────

def test_company_id_mode_uses_fetchone():
    company_id = str(uuid.uuid4())
    row = _make_company_row(company_id=uuid.UUID(company_id))
    db = MagicMock()
    db.execute.return_value.fetchone.return_value = row

    result = run_contactability_enrichment(
        db=db,
        limit=1,
        company_id=company_id,
        dry_run=True,
        apply=False,
    )

    assert result["dry_run"] is True
    assert result["companies_would_enrich"] == 1
    db.execute.return_value.fetchone.assert_called()


# ── Test 10: source='sam' updates SAM fields via mocked provider ──────────────

def test_apply_source_sam_updates_sam_fields():
    rows = [_make_company_row(uei="ABC123DEF456")]
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = rows
    db.execute.return_value.scalar_one_or_none.return_value = None  # no existing CC

    mock_sam = MagicMock()
    mock_sam.lookup_entity.return_value = SAMResult(
        uei="ABC123DEF456",
        match_status="matched",
        registration_status="Active",
        address="100 Main St, Austin, TX, 78701, USA",
    )
    mock_search = MagicMock()

    result = run_contactability_enrichment(
        db=db,
        limit=1,
        tier="warm",
        source="sam",
        dry_run=False,
        apply=True,
        search_provider=mock_search,
        sam_provider=mock_sam,
    )

    # SAM lookup ran; website search did NOT (source='sam').
    mock_sam.lookup_entity.assert_called_once()
    mock_search.find_company_website.assert_not_called()

    # The persisted CompanyContactability carries the SAM fields.
    cc = next(
        c.args[0]
        for c in db.add.call_args_list
        if isinstance(c.args[0], CompanyContactability)
    )
    assert cc.sam_uei == "ABC123DEF456"
    assert cc.sam_match_status == "matched"
    assert cc.sam_registration_status == "Active"
    assert cc.sam_address == "100 Main St, Austin, TX, 78701, USA"
    assert cc.official_website is None  # website fields untouched for source='sam'
    assert cc.contactability_status == "needs_paid_enrichment"  # SAM match, no website
    assert "sam_source=" in (cc.contactability_notes or "")
    assert result["companies_enriched"] == 1


# ── Test 11: SAM 429/rate_limited is a provider failure, not "not_contactable" ─

def _apply_sam_only(db, sam_result):
    """Run an apply-mode source='sam' enrichment with a stubbed SAM result."""
    mock_sam = MagicMock()
    mock_sam.lookup_entity.return_value = sam_result
    return run_contactability_enrichment(
        db=db,
        limit=1,
        tier="warm",
        source="sam",
        dry_run=False,
        apply=True,
        search_provider=MagicMock(),
        sam_provider=mock_sam,
    )


def _cc_rows_added(db):
    return [
        c.args[0]
        for c in db.add.call_args_list
        if isinstance(c.args[0], CompanyContactability)
    ]


def test_sam_rate_limited_counts_failed_and_writes_no_row():
    rows = [_make_company_row(uei="ABC123DEF456")]
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = rows
    db.execute.return_value.scalar_one_or_none.return_value = None

    result = _apply_sam_only(
        db, SAMResult(uei="ABC123DEF456", match_status="rate_limited")
    )

    assert result["companies_attempted"] == 1
    assert result["companies_enriched"] == 0
    assert result["companies_failed"] == 1
    assert result["companies_failed_breakdown"] == {"rate_limited": 1}
    # A throttle is NOT proof of non-contactability: no row written/overwritten.
    assert _cc_rows_added(db) == []


def test_sam_error_counts_failed_and_writes_no_row():
    rows = [_make_company_row(uei="ABC123DEF456")]
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = rows
    db.execute.return_value.scalar_one_or_none.return_value = None

    result = _apply_sam_only(db, SAMResult(uei="ABC123DEF456", match_status="error"))

    assert result["companies_enriched"] == 0
    assert result["companies_failed"] == 1
    assert result["companies_failed_breakdown"] == {"error": 1}
    assert _cc_rows_added(db) == []


def test_sam_not_found_is_a_useful_result_and_enriches():
    # not_found means the API answered "no such entity" — a real, storable fact.
    rows = [_make_company_row(uei="ABC123DEF456")]
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = rows
    db.execute.return_value.scalar_one_or_none.return_value = None

    result = _apply_sam_only(db, SAMResult(uei="ABC123DEF456", match_status="not_found"))

    assert result["companies_enriched"] == 1
    assert result["companies_failed"] == 0
    cc_rows = _cc_rows_added(db)
    assert len(cc_rows) == 1
    assert cc_rows[0].sam_match_status == "not_found"
    # No website, no SAM match → not_contactable is honest here (API really answered).
    assert cc_rows[0].contactability_status == "not_contactable"


def test_run_summary_failed_breakdown_mixed():
    rows = [
        _make_company_row(uei="A"),
        _make_company_row(uei="B"),
        _make_company_row(uei="C"),
    ]
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = rows
    db.execute.return_value.scalar_one_or_none.return_value = None

    mock_sam = MagicMock()
    mock_sam.lookup_entity.side_effect = [
        SAMResult(uei="A", match_status="matched", registration_status="Active"),
        SAMResult(uei="B", match_status="rate_limited"),
        SAMResult(uei="C", match_status="error"),
    ]

    result = run_contactability_enrichment(
        db=db,
        limit=3,
        tier="warm",
        source="sam",
        dry_run=False,
        apply=True,
        search_provider=MagicMock(),
        sam_provider=mock_sam,
    )

    assert result["companies_attempted"] == 3
    assert result["companies_enriched"] == 1  # only the matched one
    assert result["companies_failed"] == 2
    assert result["companies_failed_breakdown"] == {"rate_limited": 1, "error": 1}
    # Exactly one contactability row written (the matched company).
    assert len(_cc_rows_added(db)) == 1
