"""
Tests for app/export/csv_export.py — Task 10.

All tests use MagicMock DB sessions. No real database or real files are required
except temporary CSV output written to pytest's tmp_path fixture.

Test coverage (13 required tests):
  1.  Approved lead is exported
  2.  Lead with no review decision is not exported
  3.  Lead with latest action reject is not exported
  4.  Lead approved first but later archived is not exported
  5.  Latest approve after earlier reject is exported
  6.  CSV includes company and score fields
  7.  CSV includes evidence_urls
  8.  CSV includes signal_types
  9.  Export creates file at output_path
  10. Empty eligible set still creates CSV with headers
  11. Function does not update review_decisions
  12. Function does not update lead_candidates
  13. Salesforce sync is not called or created
"""
from __future__ import annotations

import csv
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.db.models import (
    Company,
    EvidenceItem,
    LeadCandidate,
    LeadScore,
    RawSourceEvent,
    ReviewDecision,
    Signal,
)
from app.export.csv_export import (
    CSV_FIELDNAMES,
    build_export_row,
    export_approved_leads_csv,
    get_latest_review_decision,
    is_export_eligible,
)


# ─── Mock helpers ─────────────────────────────────────────────────────────────


def _mk_all(items: list):
    """Mock a db.execute() result whose .scalars().all() returns items."""
    r = MagicMock()
    r.scalars.return_value.all.return_value = list(items)
    return r


def _mk_first(item):
    """Mock a db.execute() result whose .scalars().first() returns item."""
    r = MagicMock()
    r.scalars.return_value.first.return_value = item
    return r


def _make_lead(
    *,
    lead_id=None,
    company_id=None,
    tier="hot",
    current_score=80,
    sales_status="research",
):
    lead = MagicMock(spec=LeadCandidate)
    lead.id = lead_id or uuid.uuid4()
    lead.company_id = company_id or uuid.uuid4()
    lead.status = "active"
    lead.tier = tier
    lead.current_score = current_score
    lead.sales_status = sales_status
    lead.deleted_at = None
    return lead


def _make_decision(action: str, reviewer_id: str = "alice@portercap.net"):
    d = MagicMock(spec=ReviewDecision)
    d.action = action
    d.decided_at = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    d.reviewer_id = reviewer_id
    return d


def _make_company(*, company_id=None):
    c = MagicMock(spec=Company)
    c.id = company_id or uuid.uuid4()
    c.canonical_name = "Acme Federal Services"
    c.state = "TX"
    c.website_domain = "acmefederal.com"
    c.naics_code = "541511"
    c.industry = "IT Services"
    return c


def _make_score():
    s = MagicMock(spec=LeadScore)
    s.total_score = 80
    s.tier = "hot"
    s.component_breakdown = {"porter_fit": {"points": 25, "max": 25}}
    return s


def _make_evidence(*, source_url: str = "https://usaspending.gov/award/A1", raw_event_id=None):
    e = MagicMock(spec=EvidenceItem)
    e.source_url = source_url
    e.raw_event_id = raw_event_id
    return e


def _make_signal(signal_type: str = "CONTRACT_AWARD"):
    s = MagicMock(spec=Signal)
    s.signal_type = signal_type
    return s


def _make_raw_event(source_record_id: str = "AWARD-2024-001"):
    r = MagicMock(spec=RawSourceEvent)
    r.source_record_id = source_record_id
    return r


# ─── Test 1: Approved lead is exported ────────────────────────────────────────


def test_approved_lead_is_exported(tmp_path):
    db = MagicMock()
    lead = _make_lead()
    decision = _make_decision("approve")
    company = _make_company(company_id=lead.company_id)
    score = _make_score()
    raw_eid = uuid.uuid4()
    evidence = _make_evidence(raw_event_id=raw_eid)
    signal = _make_signal()
    raw_event = _make_raw_event()

    db.execute.side_effect = [
        _mk_all([lead]),          # all active leads
        _mk_first(decision),      # latest review decision
        _mk_first(score),         # latest score
        _mk_all([evidence]),      # evidence items
        _mk_all([signal]),        # signals
        _mk_all([raw_event]),     # raw source events
    ]
    db.get.return_value = company

    output_path = str(tmp_path / "export.csv")
    result = export_approved_leads_csv(output_path, db)

    assert result["exported_count"] == 1
    assert result["skipped_count"] == 0
    assert result["output_path"] == output_path


# ─── Test 2: Lead with no review decision is not exported ─────────────────────


def test_no_review_decision_not_exported(tmp_path):
    db = MagicMock()
    lead = _make_lead()

    db.execute.side_effect = [
        _mk_all([lead]),     # all active leads
        _mk_first(None),     # no review decision
    ]

    output_path = str(tmp_path / "export.csv")
    result = export_approved_leads_csv(output_path, db)

    assert result["exported_count"] == 0
    assert result["skipped_count"] == 1


# ─── Test 3: Lead with latest action reject is not exported ───────────────────


def test_latest_action_reject_not_exported(tmp_path):
    db = MagicMock()
    lead = _make_lead()
    decision = _make_decision("reject")

    db.execute.side_effect = [
        _mk_all([lead]),
        _mk_first(decision),
    ]

    output_path = str(tmp_path / "export.csv")
    result = export_approved_leads_csv(output_path, db)

    assert result["exported_count"] == 0
    assert result["skipped_count"] == 1


# ─── Test 4: Lead approved first but later archived is not exported ───────────
#
# review_decisions is append-only. A prior "approve" row still exists, but the
# latest row is "archive". get_latest_review_decision returns the latest; the
# lead is therefore not eligible.


def test_approved_then_archived_not_exported(tmp_path):
    db = MagicMock()
    lead = _make_lead()
    # Latest decision is "archive" — the earlier "approve" is in history but
    # get_latest_review_decision (ORDER BY decided_at DESC LIMIT 1) returns this.
    latest_decision = _make_decision("archive")

    db.execute.side_effect = [
        _mk_all([lead]),
        _mk_first(latest_decision),
    ]

    output_path = str(tmp_path / "export.csv")
    result = export_approved_leads_csv(output_path, db)

    assert result["exported_count"] == 0
    assert result["skipped_count"] == 1


# ─── Test 5: Latest approve after earlier reject is exported ──────────────────
#
# An earlier "reject" row exists in review_decisions, but the latest row is
# "approve". get_latest_review_decision returns "approve" → lead is eligible.


def test_latest_approve_after_earlier_reject_is_exported(tmp_path):
    db = MagicMock()
    lead = _make_lead()
    # Latest decision is "approve" — the earlier "reject" is in history
    latest_decision = _make_decision("approve")
    company = _make_company(company_id=lead.company_id)
    score = _make_score()
    evidence = _make_evidence()   # raw_event_id=None → no 6th db.execute call
    signal = _make_signal()

    db.execute.side_effect = [
        _mk_all([lead]),
        _mk_first(latest_decision),
        _mk_first(score),
        _mk_all([evidence]),
        _mk_all([signal]),
    ]
    db.get.return_value = company

    output_path = str(tmp_path / "export.csv")
    result = export_approved_leads_csv(output_path, db)

    assert result["exported_count"] == 1
    assert result["skipped_count"] == 0


# ─── Test 6: CSV includes company and score fields ────────────────────────────


def test_csv_includes_company_and_score_fields(tmp_path):
    db = MagicMock()
    lead = _make_lead(tier="hot", current_score=80, sales_status="research")
    decision = _make_decision("approve", reviewer_id="bob@portercap.net")
    company = _make_company(company_id=lead.company_id)
    score = _make_score()
    evidence = _make_evidence()
    signal = _make_signal()

    db.execute.side_effect = [
        _mk_all([lead]),
        _mk_first(decision),
        _mk_first(score),
        _mk_all([evidence]),
        _mk_all([signal]),
    ]
    db.get.return_value = company

    output_path = str(tmp_path / "export.csv")
    export_approved_leads_csv(output_path, db)

    with open(output_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)

    assert len(rows) == 1
    row = rows[0]
    assert row["company_name"] == "Acme Federal Services"
    assert row["state"] == "TX"
    assert row["website_domain"] == "acmefederal.com"
    assert row["naics_code"] == "541511"
    assert row["tier"] == "hot"
    assert row["current_score"] == "80"
    assert row["sales_status"] == "research"
    assert row["score_total"] == "80"
    assert row["score_tier"] == "hot"
    assert row["reviewer_id"] == "bob@portercap.net"
    assert row["latest_review_action"] == "approve"
    assert row["component_breakdown"] != ""


# ─── Test 7: CSV includes evidence_urls ───────────────────────────────────────


def test_csv_includes_evidence_urls(tmp_path):
    db = MagicMock()
    lead = _make_lead()
    decision = _make_decision("approve")
    company = _make_company(company_id=lead.company_id)
    score = _make_score()

    url_a = "https://usaspending.gov/award/A1"
    url_b = "https://usaspending.gov/award/A2"
    evidence_a = _make_evidence(source_url=url_a)
    evidence_b = _make_evidence(source_url=url_b)

    signal = _make_signal()

    db.execute.side_effect = [
        _mk_all([lead]),
        _mk_first(decision),
        _mk_first(score),
        _mk_all([evidence_a, evidence_b]),
        _mk_all([signal]),
    ]
    db.get.return_value = company

    output_path = str(tmp_path / "export.csv")
    export_approved_leads_csv(output_path, db)

    with open(output_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)

    assert len(rows) == 1
    urls = rows[0]["evidence_urls"].split("|")
    assert url_a in urls
    assert url_b in urls


# ─── Test 8: CSV includes signal_types ────────────────────────────────────────


def test_csv_includes_signal_types(tmp_path):
    db = MagicMock()
    lead = _make_lead()
    decision = _make_decision("approve")
    company = _make_company(company_id=lead.company_id)
    score = _make_score()
    evidence = _make_evidence()

    sig_contract = _make_signal("CONTRACT_AWARD")
    sig_growth = _make_signal("REVENUE_GROWTH")

    db.execute.side_effect = [
        _mk_all([lead]),
        _mk_first(decision),
        _mk_first(score),
        _mk_all([evidence]),
        _mk_all([sig_contract, sig_growth]),
    ]
    db.get.return_value = company

    output_path = str(tmp_path / "export.csv")
    export_approved_leads_csv(output_path, db)

    with open(output_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)

    assert len(rows) == 1
    signal_types = rows[0]["signal_types"].split("|")
    assert "CONTRACT_AWARD" in signal_types
    assert "REVENUE_GROWTH" in signal_types


# ─── Test 9: Export creates file at output_path ───────────────────────────────


def test_export_creates_file_at_output_path(tmp_path):
    db = MagicMock()
    db.execute.side_effect = [_mk_all([])]  # no active leads

    output_path = str(tmp_path / "subdir" / "leads.csv")
    assert not Path(output_path).exists()

    export_approved_leads_csv(output_path, db)

    assert Path(output_path).exists()
    assert Path(output_path).is_file()


# ─── Test 10: Empty eligible set still creates CSV with headers ───────────────


def test_empty_eligible_set_creates_csv_with_headers(tmp_path):
    db = MagicMock()
    db.execute.side_effect = [_mk_all([])]  # no active leads

    output_path = str(tmp_path / "empty.csv")
    result = export_approved_leads_csv(output_path, db)

    assert result["exported_count"] == 0
    assert Path(output_path).exists()

    with open(output_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames == CSV_FIELDNAMES
        rows = list(reader)

    assert rows == []


# ─── Test 11: Function does not update review_decisions ───────────────────────


def test_function_does_not_update_review_decisions(tmp_path):
    """Export is read-only — no INSERT or UPDATE on review_decisions."""
    db = MagicMock()
    lead = _make_lead()
    decision = _make_decision("approve")
    company = _make_company(company_id=lead.company_id)
    score = _make_score()
    evidence = _make_evidence()
    signal = _make_signal()

    db.execute.side_effect = [
        _mk_all([lead]),
        _mk_first(decision),
        _mk_first(score),
        _mk_all([evidence]),
        _mk_all([signal]),
    ]
    db.get.return_value = company

    export_approved_leads_csv(str(tmp_path / "out.csv"), db)

    # No row was added or committed — export is read-only
    db.add.assert_not_called()
    db.commit.assert_not_called()

    # Specifically: no ReviewDecision was constructed and inserted
    for call in db.add.call_args_list:
        assert not isinstance(call.args[0], ReviewDecision)


# ─── Test 12: Function does not update lead_candidates ────────────────────────


def test_function_does_not_update_lead_candidates(tmp_path):
    """Export is read-only — no INSERT or UPDATE on lead_candidates."""
    db = MagicMock()
    lead = _make_lead()
    decision = _make_decision("approve")
    company = _make_company(company_id=lead.company_id)
    score = _make_score()
    evidence = _make_evidence()
    signal = _make_signal()

    db.execute.side_effect = [
        _mk_all([lead]),
        _mk_first(decision),
        _mk_first(score),
        _mk_all([evidence]),
        _mk_all([signal]),
    ]
    db.get.return_value = company

    export_approved_leads_csv(str(tmp_path / "out.csv"), db)

    db.add.assert_not_called()
    db.commit.assert_not_called()

    # Specifically: no LeadCandidate was constructed and inserted
    for call in db.add.call_args_list:
        assert not isinstance(call.args[0], LeadCandidate)


# ─── Test 13: Salesforce sync is not called or created ────────────────────────


def test_salesforce_sync_not_called(tmp_path):
    """Export never creates salesforce_sync_logs rows or calls the Salesforce API."""
    db = MagicMock()
    lead = _make_lead()
    decision = _make_decision("approve")
    company = _make_company(company_id=lead.company_id)
    score = _make_score()
    evidence = _make_evidence()
    signal = _make_signal()

    db.execute.side_effect = [
        _mk_all([lead]),
        _mk_first(decision),
        _mk_first(score),
        _mk_all([evidence]),
        _mk_all([signal]),
    ]
    db.get.return_value = company

    result = export_approved_leads_csv(str(tmp_path / "out.csv"), db)

    # No database writes of any kind
    db.add.assert_not_called()
    db.commit.assert_not_called()

    # Result dict has no Salesforce-related keys
    assert "salesforce" not in result
    assert "sync" not in result

    # Module namespace contains no salesforce-related names
    import app.export.csv_export as csv_mod

    salesforce_attrs = [name for name in dir(csv_mod) if "salesforce" in name.lower()]
    assert salesforce_attrs == [], f"Unexpected salesforce names in module: {salesforce_attrs}"


# ─── Unit tests for pure helper functions ─────────────────────────────────────


def test_is_export_eligible_none_returns_false():
    assert is_export_eligible(None) is False


def test_is_export_eligible_approve_returns_true():
    d = MagicMock()
    d.action = "approve"
    assert is_export_eligible(d) is True


@pytest.mark.parametrize("action", ["reject", "archive", "mark_duplicate", "needs_research", "add_note"])
def test_is_export_eligible_non_approve_returns_false(action):
    d = MagicMock()
    d.action = action
    assert is_export_eligible(d) is False


def test_build_export_row_with_no_score():
    lead = _make_lead(tier="warm", current_score=55)
    company = _make_company()
    decision = _make_decision("approve")

    row = build_export_row(
        lead=lead,
        company=company,
        latest_decision=decision,
        score=None,
        evidence_items=[],
        signals=[],
        source_record_ids=[],
    )

    assert row["score_total"] == ""
    assert row["score_tier"] == ""
    assert row["component_breakdown"] == ""
    assert row["evidence_urls"] == ""
    assert row["signal_types"] == ""
    assert row["source_record_ids"] == ""


def test_build_export_row_signal_types_are_sorted_and_deduplicated():
    lead = _make_lead()
    company = _make_company()
    decision = _make_decision("approve")
    score = _make_score()

    sig_z = _make_signal("ZEBRA_SIGNAL")
    sig_a = _make_signal("APPLE_SIGNAL")
    sig_a_dup = _make_signal("APPLE_SIGNAL")

    row = build_export_row(
        lead=lead,
        company=company,
        latest_decision=decision,
        score=score,
        evidence_items=[],
        signals=[sig_z, sig_a, sig_a_dup],
        source_record_ids=[],
    )

    signal_types = row["signal_types"].split("|")
    assert signal_types == sorted(set(["ZEBRA_SIGNAL", "APPLE_SIGNAL"]))


def test_get_latest_review_decision_queries_db():
    db = MagicMock()
    lead_id = uuid.uuid4()
    mock_decision = _make_decision("approve")
    db.execute.return_value.scalars.return_value.first.return_value = mock_decision

    result = get_latest_review_decision(lead_id, db)

    assert result is mock_decision
    db.execute.assert_called_once()
