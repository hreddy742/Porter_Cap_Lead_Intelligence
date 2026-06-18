"""
Tests for app/ops/manual_review_pack.py

All tests use MagicMock DB sessions or pure-function calls.
No real database or network access required.
Temporary file output uses pytest's tmp_path fixture.

Tests:
  1.  default_limit_is_50
  2.  limit_can_be_overridden
  3.  tier_filter_passed_correctly
  4.  archived_leads_excluded_by_active_filter
  5.  output_fields_are_present
  6.  missing_values_become_not_available
  7.  sorting_sql_uses_tier_score_award_strength
  8.  markdown_contains_caution_language
  9.  markdown_does_not_make_positive_sales_claims
  10. empty_result_handled_safely
  11. csv_writer_creates_expected_fields
  12. markdown_writer_creates_expected_sections
"""
from __future__ import annotations

import csv
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.ops.manual_review_pack import (
    CSV_FIELDNAMES,
    NOT_AVAILABLE,
    _classify_pass_type,
    _leads_query_sql,
    build_review_rows,
    write_csv,
    write_markdown,
)
from decimal import Decimal


# ── Mock helpers ───────────────────────────────────────────────────────────────

def _mk_fetchall(items: list):
    """Mock db.execute() whose .fetchall() returns items."""
    r = MagicMock()
    r.fetchall.return_value = list(items)
    return r


def _mk_fetchone(item):
    """Mock db.execute() whose .fetchone() returns item."""
    r = MagicMock()
    r.fetchone.return_value = item
    return r


def _make_lead_row(**kwargs) -> SimpleNamespace:
    """Create a mock DB result row for the main leads query."""
    defaults = {
        "lead_candidate_id": uuid.uuid4(),
        "company_id": uuid.uuid4(),
        "tier": "warm",
        "current_score": 60,
        "sales_status": "research",
        "canonical_name": "Acme Federal Services",
        "city": "Austin",
        "state": "TX",
        "naics_code": "541511",
        "naics_description": "Custom Computer Programming Services",
        "positive_count": 2,
        "largest_single": Decimal("50000"),
        "recent_total_90d": Decimal("75000"),
        "lifetime_total": Decimal("125000"),
        "most_recent_date": "2024-01-15",
        "contactability_status": None,
        "contactability_score": None,
        "sam_match_status": None,
        "sam_registration_status": None,
        "sam_uei": None,
        "contactability_last_checked_at": None,
        "contactability_notes": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _make_evidence_row(**kwargs) -> SimpleNamespace:
    """Create a mock DB result row for the best-evidence query."""
    defaults = {
        "source_url": "https://usaspending.gov/award/TEST-001",
        "confidence_score": "0.85",
        "claim_supported": "CONTRACT_AWARD",
        "awarding_agency": "DEPT OF DEFENSE",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _make_review_row(**overrides) -> dict:
    """Create a pre-built review row dict (as returned by build_review_rows)."""
    row = {
        "rank": 1,
        "company_name": "Acme Federal Services",
        "tier": "warm",
        "current_score": "60",
        "review_status": "research",
        "state": "TX",
        "city": "Austin",
        "naics_code": "541511",
        "naics_description": "Custom Computer Programming Services",
        "largest_single_award": "$50,000",
        "award_total_90d": "$75,000",
        "lifetime_award_total": "$125,000",
        "positive_award_count": "2",
        "most_recent_award_date": "2024-01-15",
        "gate10_pass_type": "single_award_pass",
        "primary_agency": "DEPT OF DEFENSE",
        "best_evidence_claim": "CONTRACT_AWARD",
        "best_evidence_confidence": "0.85",
        "source_url": "https://usaspending.gov/award/TEST-001",
        "why_this_lead_is_included": (
            "Award evidence: largest single award $50,000. "
            "Gate 10: single_award_pass. Needs verification before outreach."
        ),
        "reviewer_notes": "",
    }
    row.update(overrides)
    return row


def _make_meta(**overrides) -> dict:
    meta = {
        "generated_at": "2024-01-15 10:00:00 UTC",
        "database": NOT_AVAILABLE,
        "limit": 50,
        "active_count": 1,
        "leads_exported": 1,
        "tier_distribution": {"warm": 1},
    }
    meta.update(overrides)
    return meta


# ── Test 1: default limit is 50 ───────────────────────────────────────────────

def test_default_limit_is_50():
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = []

    build_review_rows(db)

    first_call = db.execute.call_args_list[0]
    params = first_call.args[1]
    assert params["limit"] == 50


# ── Test 2: limit can be overridden ───────────────────────────────────────────

def test_limit_can_be_overridden():
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = []

    build_review_rows(db, limit=10)

    first_call = db.execute.call_args_list[0]
    params = first_call.args[1]
    assert params["limit"] == 10


# ── Test 3: tier filter passed correctly ──────────────────────────────────────

def test_tier_filter_passed_correctly():
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = []

    build_review_rows(db, tier="warm")

    first_call = db.execute.call_args_list[0]
    params = first_call.args[1]
    assert params.get("tier") == "warm"

    sql = _leads_query_sql("warm")
    assert "lc.tier = :tier" in sql


def test_no_tier_filter_when_tier_is_none():
    params_no_tier: dict = {"limit": 50}
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = []

    build_review_rows(db, tier=None)

    first_call = db.execute.call_args_list[0]
    params = first_call.args[1]
    assert "tier" not in params

    sql = _leads_query_sql(None)
    assert "lc.tier = :tier" not in sql


# ── Test 4: archived leads excluded by active filter ─────────────────────────

def test_archived_leads_excluded_by_active_filter():
    sql = _leads_query_sql(None)
    assert "lc.status = 'active'" in sql
    assert "lc.deleted_at IS NULL" in sql
    assert "c.deleted_at IS NULL" in sql


# ── Test 5: output fields are present ────────────────────────────────────────

def test_output_fields_are_present():
    lead_row = _make_lead_row()
    ev_row = _make_evidence_row()

    db = MagicMock()
    db.execute.side_effect = [
        _mk_fetchall([lead_row]),
        _mk_fetchone(ev_row),
    ]

    rows = build_review_rows(db, limit=1)

    assert len(rows) == 1
    for field in CSV_FIELDNAMES:
        assert field in rows[0], f"Missing field in review row: {field!r}"


# ── Test 6: missing values become NOT_AVAILABLE ───────────────────────────────

def test_missing_values_become_not_available():
    lead_row = _make_lead_row(
        tier=None,
        current_score=None,
        city=None,
        naics_code=None,
        naics_description=None,
        largest_single=None,
        recent_total_90d=None,
        lifetime_total=None,
        most_recent_date=None,
    )

    db = MagicMock()
    db.execute.side_effect = [
        _mk_fetchall([lead_row]),
        _mk_fetchone(None),  # no evidence
    ]

    rows = build_review_rows(db, limit=1)

    assert len(rows) == 1
    r = rows[0]
    assert r["tier"] == NOT_AVAILABLE
    assert r["city"] == NOT_AVAILABLE
    assert r["naics_code"] == NOT_AVAILABLE
    assert r["largest_single_award"] == NOT_AVAILABLE
    assert r["award_total_90d"] == NOT_AVAILABLE
    assert r["lifetime_award_total"] == NOT_AVAILABLE
    assert r["most_recent_award_date"] == NOT_AVAILABLE
    assert r["primary_agency"] == NOT_AVAILABLE
    assert r["source_url"] == NOT_AVAILABLE
    assert r["best_evidence_claim"] == NOT_AVAILABLE


# ── Test 7: sorting SQL uses tier, score, and award strength ─────────────────

def test_sorting_sql_uses_tier_score_award_strength():
    sql = _leads_query_sql(None)

    assert "CASE lc.tier" in sql
    assert "'hot' THEN 1" in sql
    assert "'warm' THEN 2" in sql
    assert "'cold' THEN 3" in sql
    assert "lc.current_score DESC NULLS LAST" in sql
    assert "largest_single DESC NULLS LAST" in sql
    assert "recent_total_90d DESC NULLS LAST" in sql
    assert "most_recent_date DESC NULLS LAST" in sql


# ── Test 8: markdown contains caution language ────────────────────────────────

def test_markdown_contains_caution_language(tmp_path):
    rows = [_make_review_row()]
    md_path = str(tmp_path / "test.md")
    write_markdown(rows, _make_meta(), md_path)

    with open(md_path, encoding="utf-8") as fh:
        content = fh.read().lower()

    assert "not sales-ready" in content
    assert "contacts are not verified" in content
    assert "factoring need is not proven" in content
    assert "human review is required" in content


# ── Test 9: markdown does not make positive sales/contact/ROI claims ──────────

def test_markdown_does_not_make_positive_sales_claims(tmp_path):
    rows = [_make_review_row()]
    md_path = str(tmp_path / "test.md")
    write_markdown(rows, _make_meta(), md_path)

    with open(md_path, encoding="utf-8") as fh:
        content = fh.read().lower()

    # These positive claims must NOT appear
    assert "is sales-ready" not in content
    assert "ready for salesforce" not in content
    assert "verified contact" not in content
    assert "proves factoring need" not in content
    assert "proves roi" not in content


# ── Test 10: empty result handled safely ──────────────────────────────────────

def test_empty_result_handled_safely(tmp_path):
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = []

    rows = build_review_rows(db)
    assert rows == []

    csv_path = str(tmp_path / "empty.csv")
    write_csv(rows, csv_path)
    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames == CSV_FIELDNAMES
        assert list(reader) == []

    md_path = str(tmp_path / "empty.md")
    write_markdown(rows, _make_meta(leads_exported=0, tier_distribution={}), md_path)
    with open(md_path, encoding="utf-8") as fh:
        content = fh.read()
    assert "Manual Review Pack" in content
    assert "No active research-ready leads found" in content


# ── Test 11: CSV writer creates expected fields ───────────────────────────────

def test_csv_writer_creates_expected_fields(tmp_path):
    rows = [_make_review_row()]
    csv_path = str(tmp_path / "pack.csv")
    write_csv(rows, csv_path)

    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames == CSV_FIELDNAMES
        out_rows = list(reader)

    assert len(out_rows) == 1
    assert out_rows[0]["company_name"] == "Acme Federal Services"
    assert out_rows[0]["tier"] == "warm"
    assert out_rows[0]["reviewer_notes"] == ""


# ── Test 12: markdown writer creates expected sections ───────────────────────

def test_markdown_writer_creates_expected_sections(tmp_path):
    rows = [_make_review_row()]
    md_path = str(tmp_path / "pack.md")
    write_markdown(rows, _make_meta(), md_path)

    with open(md_path, encoding="utf-8") as fh:
        content = fh.read()

    assert "# Manual Review Pack" in content
    assert "## Summary" in content
    assert "## Lead Cards" in content
    assert "## Limitations" in content
    assert "#1" in content
    assert "Acme Federal Services" in content
    assert "Generated at:" in content
    assert "Active leads considered:" in content
    assert "Leads exported:" in content


# ── Unit tests for _classify_pass_type ───────────────────────────────────────

@pytest.mark.parametrize("largest,recent,expected", [
    (Decimal("50000"), Decimal("0"),     "single_award_pass"),
    (Decimal("0"),     Decimal("10000"), "aggregate_90d_pass"),
    (Decimal("5000"),  Decimal("5000"),  "below_threshold"),
    (Decimal("0"),     Decimal("0"),     "unknown"),
])
def test_classify_pass_type(largest, recent, expected):
    min_s = Decimal("10000")
    min_90 = Decimal("10000")
    assert _classify_pass_type(largest, recent, min_s, min_90) == expected


# ── DB read-only safety: no writes ────────────────────────────────────────────

def test_build_review_rows_does_not_write_to_db():
    lead_row = _make_lead_row()
    ev_row = _make_evidence_row()

    db = MagicMock()
    db.execute.side_effect = [
        _mk_fetchall([lead_row]),
        _mk_fetchone(ev_row),
    ]

    build_review_rows(db, limit=1)

    db.add.assert_not_called()
    db.commit.assert_not_called()
    db.delete.assert_not_called()


# ── Contactability / SAM entity validation fields ────────────────────────────

def test_leads_query_joins_contactability():
    """The leads query must LEFT JOIN company_contactability and select its fields."""
    sql = _leads_query_sql(None)

    assert "LEFT JOIN company_contactability cc" in sql
    assert "cc.contactability_status" in sql
    assert "cc.contactability_score" in sql
    assert "cc.sam_match_status" in sql
    assert "cc.sam_registration_status" in sql
    assert "cc.sam_uei" in sql
    assert "contactability_last_checked_at" in sql
    assert "cc.contactability_notes" in sql


def test_contactability_fields_present_when_enriched():
    lead_row = _make_lead_row(
        contactability_status="needs_paid_enrichment",
        contactability_score=4,
        sam_match_status="matched",
        sam_registration_status="Active",
        sam_uei="ABC123DEF456",
        contactability_last_checked_at="2026-06-15 20:24:21",
        contactability_notes="sam_source=https://api.sam.gov",
    )
    ev_row = _make_evidence_row()

    db = MagicMock()
    db.execute.side_effect = [
        _mk_fetchall([lead_row]),
        _mk_fetchone(ev_row),
    ]

    rows = build_review_rows(db, limit=1)

    r = rows[0]
    assert r["contactability_status"] == "needs_paid_enrichment"
    assert r["contactability_score"] == "4"
    assert r["sam_match_status"] == "matched"
    assert r["sam_registration_status"] == "Active"
    assert r["sam_uei"] == "ABC123DEF456"
    assert r["contactability_last_checked_at"] == "2026-06-15 20:24:21"
    assert r["contactability_notes"] == "sam_source=https://api.sam.gov"


def test_contactability_fields_not_available_when_missing():
    # _make_lead_row defaults all contactability fields to None (no enrichment row)
    lead_row = _make_lead_row()
    ev_row = _make_evidence_row()

    db = MagicMock()
    db.execute.side_effect = [
        _mk_fetchall([lead_row]),
        _mk_fetchone(ev_row),
    ]

    rows = build_review_rows(db, limit=1)

    r = rows[0]
    assert r["contactability_status"] == NOT_AVAILABLE
    assert r["contactability_score"] == NOT_AVAILABLE
    assert r["sam_match_status"] == NOT_AVAILABLE
    assert r["sam_uei"] == NOT_AVAILABLE
    assert r["contactability_notes"] == NOT_AVAILABLE


def test_markdown_includes_sam_entity_validation_section(tmp_path):
    rows = [_make_review_row(
        contactability_status="needs_paid_enrichment",
        sam_match_status="matched",
        sam_uei="ABC123DEF456",
    )]
    md_path = str(tmp_path / "pack.md")
    write_markdown(rows, _make_meta(), md_path)

    with open(md_path, encoding="utf-8") as fh:
        content = fh.read()

    assert "SAM Entity Validation" in content
    assert "SAM UEI: ABC123DEF456" in content
    # The section must not imply a confirmed contact (positive sales claims are
    # already guarded document-wide by test_markdown_does_not_make_positive_sales_claims)
    assert "verified contact" not in content.lower()
