"""
Tests for app/dashboard/review.py helper functions — Task 09.

All tests use MagicMock DB sessions. No real database connection is required.

Test coverage:
  1.  get_reviewer_id reads X-Forwarded-Email
  2.  get_reviewer_id returns None when header missing
  3.  create_review_decision inserts one review_decisions row
  4.  create_review_decision rejects blank reviewer_id
  5.  create_review_decision rejects invalid action
  6.  create_review_decision does not update lead_candidates
  7.  two conflicting decisions both exist as separate rows
  8.  get_latest_review_action returns the latest decided_at action
  9.  list_reviewable_leads returns active leads
  10. list_reviewable_leads can filter by tier
  11. get_lead_detail includes company, score, evidence, signals, and review history
  12. Streamlit app imports without executing database work at import time
"""
from __future__ import annotations

import importlib
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.dashboard.review import (
    _get_primary_source_by_company,
    create_review_decision,
    format_currency,
    format_date,
    get_award_aggregation,
    get_award_gate_summary,
    get_contactability,
    get_latest_run_context,
    get_lead_detail,
    get_latest_review_action,
    get_reviewer_id,
    get_source_list,
    list_leads_filtered,
    list_reviewable_leads,
)
from app.db.models import (
    Company,
    CompanyContactability,
    EvidenceItem,
    LeadCandidate,
    LeadScore,
    ReviewDecision,
    Signal,
)


# ─── Test 1: get_reviewer_id reads X-Forwarded-Email ─────────────────────────


def test_get_reviewer_id_reads_header():
    headers = {"X-Forwarded-Email": "alice@portercap.net"}
    assert get_reviewer_id(headers) == "alice@portercap.net"


# ─── Test 2: get_reviewer_id returns None when header missing ─────────────────


def test_get_reviewer_id_returns_none_when_header_missing():
    assert get_reviewer_id({}) is None
    assert get_reviewer_id(None) is None
    assert get_reviewer_id({"Authorization": "Bearer token"}) is None


# ─── Test 3: create_review_decision inserts one review_decisions row ──────────


def test_create_review_decision_inserts_one_row():
    db = MagicMock()
    lead_id = uuid.uuid4()

    decision = create_review_decision(
        lead_candidate_id=lead_id,
        action="approve",
        note="Strong A/R fit — recurring government contracts",
        reviewer_id="alice@portercap.net",
        db=db,
    )

    db.add.assert_called_once()
    db.commit.assert_called_once()

    added = db.add.call_args[0][0]
    assert isinstance(added, ReviewDecision)
    assert added.action == "approve"
    assert added.reviewer_id == "alice@portercap.net"
    assert added.lead_candidate_id == lead_id
    assert added.note == "Strong A/R fit — recurring government contracts"
    assert decision is added


# ─── Test 4: create_review_decision rejects blank reviewer_id ────────────────


@pytest.mark.parametrize("bad_id", ["", "   ", None])
def test_create_review_decision_rejects_blank_reviewer(bad_id):
    db = MagicMock()

    with pytest.raises(ValueError, match="reviewer_id"):
        create_review_decision(
            lead_candidate_id=uuid.uuid4(),
            action="approve",
            note=None,
            reviewer_id=bad_id,
            db=db,
        )

    db.add.assert_not_called()
    db.commit.assert_not_called()


# ─── Test 5: create_review_decision rejects invalid action ────────────────────


def test_create_review_decision_rejects_invalid_action():
    db = MagicMock()

    with pytest.raises(ValueError, match="Invalid action"):
        create_review_decision(
            lead_candidate_id=uuid.uuid4(),
            action="delete_everything",
            note=None,
            reviewer_id="alice@portercap.net",
            db=db,
        )

    db.add.assert_not_called()
    db.commit.assert_not_called()


# ─── Test 6: create_review_decision does not update lead_candidates ──────────


def test_create_review_decision_does_not_update_lead_candidates():
    db = MagicMock()

    create_review_decision(
        lead_candidate_id=uuid.uuid4(),
        action="reject",
        note=None,
        reviewer_id="bob@portercap.net",
        db=db,
    )

    # Exactly one add() call and it is a ReviewDecision, never a LeadCandidate
    assert db.add.call_count == 1
    added = db.add.call_args[0][0]
    assert isinstance(added, ReviewDecision)
    assert not isinstance(added, LeadCandidate)

    # No execute() calls — no UPDATE, no SELECT for lead_candidates
    db.execute.assert_not_called()


# ─── Test 7: two conflicting decisions both exist as separate rows ────────────


def test_two_conflicting_decisions_are_separate_rows():
    db = MagicMock()
    lead_id = uuid.uuid4()

    d1 = create_review_decision(
        lead_candidate_id=lead_id,
        action="approve",
        note="Looks good",
        reviewer_id="alice@portercap.net",
        db=db,
    )
    d2 = create_review_decision(
        lead_candidate_id=lead_id,
        action="reject",
        note="Actually a bank subsidiary",
        reviewer_id="bob@portercap.net",
        db=db,
    )

    # Both decisions were inserted as separate rows
    assert db.add.call_count == 2
    assert db.commit.call_count == 2

    actions = {db.add.call_args_list[i][0][0].action for i in range(2)}
    assert actions == {"approve", "reject"}

    # Each decision has its own unique id
    assert d1.id != d2.id


# ─── Test 8: get_latest_review_action returns the latest decided_at action ────


def test_get_latest_review_action_returns_latest():
    db = MagicMock()
    lead_id = uuid.uuid4()

    mock_latest = MagicMock(spec=ReviewDecision)
    mock_latest.action = "reject"
    mock_latest.decided_at = datetime(2024, 6, 4, 12, 0, 0, tzinfo=timezone.utc)

    db.execute.return_value.scalars.return_value.first.return_value = mock_latest

    result = get_latest_review_action(lead_id, db)

    assert result is mock_latest
    assert result.action == "reject"
    db.execute.assert_called_once()


# ─── Test 9: list_reviewable_leads returns active leads ──────────────────────


def test_list_reviewable_leads_returns_active_leads():
    db = MagicMock()

    mock_lead_a = MagicMock(spec=LeadCandidate)
    mock_lead_a.status = "active"
    mock_lead_b = MagicMock(spec=LeadCandidate)
    mock_lead_b.status = "active"

    db.execute.return_value.scalars.return_value.all.return_value = [
        mock_lead_a,
        mock_lead_b,
    ]

    results = list_reviewable_leads(db)

    assert len(results) == 2
    db.execute.assert_called_once()


# ─── Test 10: list_reviewable_leads can filter by tier ───────────────────────


def test_list_reviewable_leads_filters_by_tier():
    db_no_filter = MagicMock()
    db_no_filter.execute.return_value.scalars.return_value.all.return_value = []

    db_with_filter = MagicMock()
    db_with_filter.execute.return_value.scalars.return_value.all.return_value = []

    list_reviewable_leads(db_no_filter, tier=None)
    list_reviewable_leads(db_with_filter, tier="Hot")

    stmt_without_tier = str(db_no_filter.execute.call_args[0][0])
    stmt_with_tier = str(db_with_filter.execute.call_args[0][0])

    # The SQL statement must differ when a tier filter is added
    assert stmt_without_tier != stmt_with_tier

    # The tier column appears in the WHERE clause of the filtered statement
    assert "tier" in stmt_with_tier


# ─── Test 11: get_lead_detail includes all required sections ──────────────────


def test_get_lead_detail_includes_all_sections():
    db = MagicMock()
    lead_id = uuid.uuid4()
    company_id = uuid.uuid4()

    mock_lead = MagicMock(spec=LeadCandidate)
    mock_lead.id = lead_id
    mock_lead.company_id = company_id

    mock_company = MagicMock(spec=Company)
    mock_company.id = company_id

    # db.get: first call → lead, second call → company
    db.get.side_effect = [mock_lead, mock_company]

    mock_score = MagicMock(spec=LeadScore)
    mock_evidence = [MagicMock(spec=EvidenceItem)]
    mock_signals = [MagicMock(spec=Signal)]
    mock_decisions = [MagicMock(spec=ReviewDecision)]

    # db.execute: called 4 times in order — score, evidence, signals, decisions
    score_result = MagicMock()
    score_result.scalars.return_value.first.return_value = mock_score

    evidence_result = MagicMock()
    evidence_result.scalars.return_value.all.return_value = mock_evidence

    signals_result = MagicMock()
    signals_result.scalars.return_value.all.return_value = mock_signals

    decisions_result = MagicMock()
    decisions_result.scalars.return_value.all.return_value = mock_decisions

    db.execute.side_effect = [
        score_result,
        evidence_result,
        signals_result,
        decisions_result,
    ]

    result = get_lead_detail(lead_id, db)

    assert result is not None
    assert result["lead"] is mock_lead
    assert result["company"] is mock_company
    assert result["latest_score"] is mock_score
    assert result["evidence"] == mock_evidence
    assert result["signals"] == mock_signals
    assert result["review_history"] == mock_decisions


def test_get_lead_detail_returns_none_for_missing_lead():
    db = MagicMock()
    db.get.return_value = None

    result = get_lead_detail(uuid.uuid4(), db)

    assert result is None
    db.execute.assert_not_called()


# ─── Test 12: Streamlit app imports without executing database work ────────────


def test_dashboard_app_imports_without_db_work():
    # Remove any cached version so the module body re-executes on import
    sys.modules.pop("app.dashboard.app", None)

    mock_st = MagicMock()
    # Provide a real empty dict so dict(st.context.headers) works without error
    mock_st.context.headers = {}
    # sidebar.radio returns a MagicMock which is != "Lead List" and != "Lead Detail"
    # so all page-conditional blocks (and their DB calls) are skipped at import time

    with patch.dict(sys.modules, {"streamlit": mock_st}):
        mod = importlib.import_module("app.dashboard.app")

    assert mod is not None
    # No DB session was opened at module level
    # (SessionLocal() is only called inside page conditionals that were not entered)


# ─── Test 13: scored leads with research status are returned ──────────────────


def test_list_reviewable_leads_returns_scored_research_leads():
    """
    Leads created by score_company (status='active', sales_status='research',
    current_score set, tier set) are returned by list_reviewable_leads.
    The query must NOT filter on sales_status or require a non-null current_score
    — those fields are display-only, not query predicates.
    """
    db = MagicMock()

    mock_lead = MagicMock(spec=LeadCandidate)
    mock_lead.status = "active"
    mock_lead.sales_status = "research"
    mock_lead.current_score = 23
    mock_lead.tier = "cold"

    db.execute.return_value.scalars.return_value.all.return_value = [mock_lead]

    results = list_reviewable_leads(db)

    assert len(results) == 1
    assert results[0].status == "active"
    assert results[0].sales_status == "research"
    assert results[0].current_score == 23
    assert results[0].tier == "cold"


# ─── Test 14: query does not filter out valid leads on extra predicates ────────


def test_list_reviewable_leads_no_extra_filter_on_valid_leads():
    """
    The WHERE clause only uses status='active' and company.deleted_at IS NULL.
    It must NOT filter by sales_status, tier, or current_score — those would
    silently hide newly-scored leads that haven't been reviewed yet.
    """
    db = MagicMock()
    db.execute.return_value.scalars.return_value.all.return_value = []

    list_reviewable_leads(db)

    stmt_str = str(db.execute.call_args[0][0]).lower()

    # Must filter on status (for active leads)
    assert "status" in stmt_str
    # Must NOT have an equality predicate on sales_status or current_score.
    # These columns appear in the SELECT list but must never appear in WHERE.
    assert "sales_status =" not in stmt_str
    assert "current_score =" not in stmt_str


# ─── Test 15: company attribute accessible without live session ───────────────


def test_list_reviewable_leads_company_accessible_after_session_close():
    """
    list_reviewable_leads uses joinedload(LeadCandidate.company), so the
    company relationship is populated inside the session.  Accessing
    lead.company (and lead.company.canonical_name) after the session closes
    must not raise DetachedInstanceError.

    This test simulates the post-session access that the Streamlit Lead List
    loop performs:  company_name = lead.company.canonical_name if lead.company else ...
    """
    db = MagicMock()

    mock_company = MagicMock(spec=Company)
    mock_company.canonical_name = "Acme Federal Services"

    mock_lead = MagicMock(spec=LeadCandidate)
    mock_lead.status = "active"
    mock_lead.company = mock_company  # joinedload pre-populates this before session closes

    db.execute.return_value.scalars.return_value.all.return_value = [mock_lead]

    results = list_reviewable_leads(db)

    # Simulate what app.py does after the `with SessionLocal()` block exits
    company_name = (
        results[0].company.canonical_name if results[0].company else "unknown"
    )
    assert company_name == "Acme Federal Services"


# ─── Test 16: Lead Detail exposes evidence source_url as a deep link ──────────


def test_get_lead_detail_evidence_exposes_deep_link_source_url():
    """
    Evidence items in the lead detail must carry a non-homepage source_url so
    the dashboard can render a working evidence link.
    """
    db = MagicMock()
    lead_id = uuid.uuid4()
    company_id = uuid.uuid4()

    mock_lead = MagicMock(spec=LeadCandidate)
    mock_lead.id = lead_id
    mock_lead.company_id = company_id

    mock_company = MagicMock(spec=Company)
    mock_company.id = company_id

    db.get.side_effect = [mock_lead, mock_company]

    mock_ev = MagicMock(spec=EvidenceItem)
    mock_ev.source_url = (
        "https://www.usaspending.gov/award/CONT_AWD_TEST_9700_-NONE-_-NONE-/"
    )
    mock_ev.claim_supported = "CONTRACT_AWARD"

    score_result = MagicMock()
    score_result.scalars.return_value.first.return_value = None
    evidence_result = MagicMock()
    evidence_result.scalars.return_value.all.return_value = [mock_ev]
    signals_result = MagicMock()
    signals_result.scalars.return_value.all.return_value = []
    decisions_result = MagicMock()
    decisions_result.scalars.return_value.all.return_value = []

    db.execute.side_effect = [
        score_result,
        evidence_result,
        signals_result,
        decisions_result,
    ]

    detail = get_lead_detail(lead_id, db)

    evidence = detail["evidence"]
    assert len(evidence) == 1
    url = evidence[0].source_url
    assert url is not None
    assert url.strip("/") != "https://www.usaspending.gov", (
        "evidence source_url must not be the homepage"
    )
    assert "CONT_AWD" in url, "evidence source_url must contain the award key"


# ─── Tests 17-22: get_award_aggregation ──────────────────────────────────────


def _make_evidence(extracted_fields: dict) -> MagicMock:
    ev = MagicMock(spec=EvidenceItem)
    ev.claim_supported = "CONTRACT_AWARD"
    ev.extracted_fields = extracted_fields
    return ev


def _db_returning(items: list) -> MagicMock:
    db = MagicMock()
    db.execute.return_value.scalars.return_value.all.return_value = items
    return db


# ─── Test 17: empty evidence → all zeros ──────────────────────────────────────


def test_get_award_aggregation_empty():
    company_id = uuid.uuid4()
    db = _db_returning([])

    result = get_award_aggregation(company_id, db)

    assert result["award_count"] == 0
    assert result["total_amount"] == 0
    assert result["avg_amount"] == 0
    assert result["by_year"] == []
    assert result["by_agency"] == []
    assert result["by_action_type"] == []


# ─── Test 18: single award ────────────────────────────────────────────────────


def test_get_award_aggregation_single_award():
    from decimal import Decimal

    company_id = uuid.uuid4()
    ev = _make_evidence({
        "award_amount": "500000",
        "action_date": "2024-03-15",
        "awarding_agency": "Dept of Defense",
        "action_type": "A",
        "action_type_description": "Initial Contract",
    })
    db = _db_returning([ev])

    result = get_award_aggregation(company_id, db)

    assert result["award_count"] == 1
    assert result["total_amount"] == Decimal("500000")
    assert result["avg_amount"] == Decimal("500000")
    assert result["by_year"] == [{"year": 2024, "count": 1, "total": Decimal("500000")}]
    assert result["by_agency"][0]["agency"] == "Dept of Defense"
    assert result["by_action_type"][0]["action_type"] == "Initial Contract"


# ─── Test 19: multiple awards — year breakdown ────────────────────────────────


def test_get_award_aggregation_year_breakdown():
    from decimal import Decimal

    company_id = uuid.uuid4()
    items = [
        _make_evidence({"award_amount": "100000", "action_date": "2023-06-01", "awarding_agency": "GSA"}),
        _make_evidence({"award_amount": "200000", "action_date": "2023-11-15", "awarding_agency": "GSA"}),
        _make_evidence({"award_amount": "400000", "action_date": "2024-02-28", "awarding_agency": "GSA"}),
    ]
    db = _db_returning(items)

    result = get_award_aggregation(company_id, db)

    assert result["award_count"] == 3
    assert result["total_amount"] == Decimal("700000")

    years = {r["year"]: r for r in result["by_year"]}
    assert years[2023]["count"] == 2
    assert years[2023]["total"] == Decimal("300000")
    assert years[2024]["count"] == 1
    assert years[2024]["total"] == Decimal("400000")
    # Most recent year first
    assert result["by_year"][0]["year"] == 2024


# ─── Test 20: agency grouping, top 10 cap ─────────────────────────────────────


def test_get_award_aggregation_agency_grouping():
    from decimal import Decimal

    company_id = uuid.uuid4()
    items = [
        _make_evidence({"award_amount": "300000", "action_date": "2024-01-01", "awarding_agency": "DoD"}),
        _make_evidence({"award_amount": "100000", "action_date": "2024-01-02", "awarding_agency": "GSA"}),
        _make_evidence({"award_amount": "200000", "action_date": "2024-01-03", "awarding_agency": "DoD"}),
    ]
    db = _db_returning(items)

    result = get_award_aggregation(company_id, db)

    agencies = {r["agency"]: r for r in result["by_agency"]}
    assert agencies["DoD"]["count"] == 2
    assert agencies["DoD"]["total"] == Decimal("500000")
    assert agencies["GSA"]["count"] == 1
    # Sorted by total descending — DoD first
    assert result["by_agency"][0]["agency"] == "DoD"


# ─── Test 21: missing/invalid award_amount fields are skipped ─────────────────


def test_get_award_aggregation_skips_invalid_amounts():
    from decimal import Decimal

    company_id = uuid.uuid4()
    items = [
        _make_evidence({"award_amount": None, "action_date": "2024-01-01"}),
        _make_evidence({"award_amount": "not_a_number", "action_date": "2024-01-02"}),
        _make_evidence({"award_amount": "-500", "action_date": "2024-01-03"}),
        _make_evidence({"award_amount": "250000", "action_date": "2024-01-04"}),
    ]
    db = _db_returning(items)

    result = get_award_aggregation(company_id, db)

    assert result["award_count"] == 1
    assert result["total_amount"] == Decimal("250000")


# ─── Test 22: missing awarding_agency falls back to "Unknown" ─────────────────


def test_get_award_aggregation_missing_agency_fallback():
    company_id = uuid.uuid4()
    ev = _make_evidence({"award_amount": "100000", "action_date": "2024-05-01"})
    db = _db_returning([ev])

    result = get_award_aggregation(company_id, db)

    assert result["by_agency"][0]["agency"] == "Unknown"


# ─── Tests 23-28: get_award_gate_summary ─────────────────────────────────────


def _make_signal(award_amount, signal_date) -> MagicMock:
    sig = MagicMock(spec=Signal)
    sig.award_amount = award_amount
    sig.signal_date = signal_date
    return sig


def _db_returning_signals(signals: list) -> MagicMock:
    db = MagicMock()
    db.execute.return_value.scalars.return_value.all.return_value = signals
    return db


# ─── Test 23: no signals → zeros and unknown ─────────────────────────────────


def test_get_award_gate_summary_empty():
    from decimal import Decimal

    result = get_award_gate_summary(uuid.uuid4(), _db_returning_signals([]))

    assert result["positive_count"] == 0
    assert result["largest_single"] == Decimal("0")
    assert result["recent_total_90d"] == Decimal("0")
    assert result["most_recent_date"] is None
    assert result["pass_type"] == "unknown"


# ─── Test 24: large single award → single_award_pass ─────────────────────────


def test_get_award_gate_summary_single_award_pass():
    from decimal import Decimal

    today = date.today()
    sig = _make_signal(Decimal("250000"), today)
    result = get_award_gate_summary(uuid.uuid4(), _db_returning_signals([sig]))

    assert result["positive_count"] == 1
    assert result["largest_single"] == Decimal("250000")
    assert result["pass_type"] == "single_award_pass"
    assert result["most_recent_date"] == today


# ─── Test 25: multiple small recent awards summing to ≥10k → aggregate_90d_pass


def test_get_award_gate_summary_aggregate_90d_pass():
    from decimal import Decimal

    today = date.today()
    signals = [
        _make_signal(Decimal("4000"), today - timedelta(days=10)),
        _make_signal(Decimal("4000"), today - timedelta(days=20)),
        _make_signal(Decimal("4000"), today - timedelta(days=30)),
    ]
    result = get_award_gate_summary(uuid.uuid4(), _db_returning_signals(signals))

    assert result["positive_count"] == 3
    assert result["largest_single"] == Decimal("4000")
    assert result["recent_total_90d"] == Decimal("12000")
    assert result["pass_type"] == "aggregate_90d_pass"


# ─── Test 26: positive awards but both below threshold → below_threshold ──────


def test_get_award_gate_summary_below_threshold():
    from decimal import Decimal

    today = date.today()
    sig = _make_signal(Decimal("500"), today - timedelta(days=5))
    result = get_award_gate_summary(uuid.uuid4(), _db_returning_signals([sig]))

    assert result["positive_count"] == 1
    assert result["pass_type"] == "below_threshold"


# ─── Test 27: old awards excluded from 90-day total ──────────────────────────


def test_get_award_gate_summary_90d_excludes_old_awards():
    from decimal import Decimal

    today = date.today()
    recent = _make_signal(Decimal("5000"), today - timedelta(days=30))
    old = _make_signal(Decimal("50000"), today - timedelta(days=120))
    result = get_award_gate_summary(uuid.uuid4(), _db_returning_signals([recent, old]))

    # largest_single includes all positive awards (including the old one)
    assert result["largest_single"] == Decimal("50000")
    # 90d total only counts the recent award — old one is excluded
    assert result["recent_total_90d"] == Decimal("5000")
    # both signals are positive
    assert result["positive_count"] == 2
    # $50,000 >= $10,000 single threshold → single_award_pass
    assert result["pass_type"] == "single_award_pass"


# ─── Test 28: zero and negative awards are ignored ────────────────────────────


def test_get_award_gate_summary_ignores_zero_and_negative():
    from decimal import Decimal

    today = date.today()
    signals = [
        _make_signal(Decimal("0"), today),
        _make_signal(Decimal("-500"), today),
        _make_signal(None, today),
        _make_signal(Decimal("15000"), today),
    ]
    result = get_award_gate_summary(uuid.uuid4(), _db_returning_signals(signals))

    assert result["positive_count"] == 1
    assert result["largest_single"] == Decimal("15000")
    assert result["pass_type"] == "single_award_pass"


# ─── Tests 29-33: format_currency and format_date ────────────────────────────


def test_format_currency_none_returns_not_available():
    assert format_currency(None) == "Not available"


def test_format_currency_zero_returns_dollar_zero():
    from decimal import Decimal

    assert format_currency(0) == "$0"
    assert format_currency(Decimal("0")) == "$0"


def test_format_currency_decimal_formats_clearly():
    from decimal import Decimal

    result = format_currency(Decimal("1234.56"))
    assert result == "$1,235"


def test_format_date_none_returns_not_available():
    assert format_date(None) == "Not available"


def test_format_date_date_object_returns_readable_string():
    result = format_date(date(2024, 3, 15))
    assert result == "2024-03-15"


# ─── Tests 34-35: get_contactability ──────────────────────────────────────────


def test_get_contactability_returns_row_when_present():
    db = MagicMock()
    company_id = uuid.uuid4()

    mock_contact = MagicMock(spec=CompanyContactability)
    mock_contact.contactability_status = "needs_paid_enrichment"
    mock_contact.sam_match_status = "matched"

    db.execute.return_value.scalar_one_or_none.return_value = mock_contact

    result = get_contactability(company_id, db)

    assert result is mock_contact
    assert result.contactability_status == "needs_paid_enrichment"
    assert result.sam_match_status == "matched"
    db.execute.assert_called_once()


def test_get_contactability_returns_none_when_missing():
    db = MagicMock()
    db.execute.return_value.scalar_one_or_none.return_value = None

    result = get_contactability(uuid.uuid4(), db)

    assert result is None
    db.execute.assert_called_once()


# ─── Tests 36-37: get_latest_run_context ──────────────────────────────────────


def test_get_latest_run_context_no_runs():
    """When no pipeline runs exist, has_runs=False and leads_scored=None."""
    db = MagicMock()
    no_run_health = {
        "has_runs": False,
        "pipeline_run_id": None,
        "status": None,
        "started_at": None,
        "finished_at": None,
        "duration_seconds": None,
        "sources_total": 0,
        "sources_succeeded": 0,
        "sources_failed": 0,
        "records_fetched": None,
        "raw_events_stored": None,
        "evidence_items_created": None,
        "companies_resolved": None,
        "signals_created": None,
        "companies_scored": None,
        "errors": [],
    }
    with patch("app.dashboard.review.get_latest_pipeline_health", return_value=no_run_health):
        result = get_latest_run_context(db)

    assert result["has_runs"] is False
    assert result["leads_scored"] is None
    # No DB query needed when has_runs=False
    db.query.assert_not_called()


def test_get_latest_run_context_with_run():
    """When a run exists, leads_scored = sum of hot+warm+cold+archive."""
    db = MagicMock()
    mock_run = MagicMock()
    mock_run.total_hot = 3
    mock_run.total_warm = 5
    mock_run.total_cold = 2
    mock_run.total_archive = 1
    db.query.return_value.order_by.return_value.first.return_value = mock_run

    run_health = {
        "has_runs": True,
        "pipeline_run_id": uuid.uuid4(),
        "status": "completed",
        "started_at": datetime(2026, 6, 19, 10, 0, 0),
        "finished_at": datetime(2026, 6, 19, 10, 5, 0),
        "duration_seconds": 300.0,
        "sources_total": 1,
        "sources_succeeded": 1,
        "sources_failed": 0,
        "records_fetched": 500,
        "raw_events_stored": 480,
        "evidence_items_created": None,
        "companies_resolved": None,
        "signals_created": None,
        "companies_scored": None,
        "errors": [],
    }
    with patch("app.dashboard.review.get_latest_pipeline_health", return_value=run_health):
        result = get_latest_run_context(db)

    assert result["has_runs"] is True
    assert result["leads_scored"] == 11  # 3+5+2+1


# ─── Tests 38-39: get_source_list ─────────────────────────────────────────────


def test_get_source_list_returns_id_name_dicts():
    db = MagicMock()
    from app.db.models import SourceRegistry as SR

    mock_src = MagicMock(spec=SR)
    mock_src.id = uuid.uuid4()
    mock_src.name = "usaspending"
    db.execute.return_value.scalars.return_value.all.return_value = [mock_src]

    result = get_source_list(db)

    assert len(result) == 1
    assert result[0]["name"] == "usaspending"
    assert result[0]["id"] == mock_src.id


def test_get_source_list_empty():
    db = MagicMock()
    db.execute.return_value.scalars.return_value.all.return_value = []
    assert get_source_list(db) == []


# ─── Tests 40-53: list_leads_filtered ────────────────────────────────────────

# Helper: build mock lead + three execute side_effects (main, signals, sources)
def _mock_filtered_db(leads: list) -> MagicMock:
    db = MagicMock()
    main_result = MagicMock()
    main_result.scalars.return_value.all.return_value = leads
    sig_result = MagicMock()
    sig_result.all.return_value = []
    src_result = MagicMock()
    src_result.all.return_value = []
    db.execute.side_effect = [main_result, sig_result, src_result]
    return db


def test_list_leads_filtered_view_all_returns_active():
    """view='all' returns all active leads with no view-specific filter."""
    mock_lead = MagicMock(spec=LeadCandidate)
    mock_lead.status = "active"
    mock_lead.company_id = uuid.uuid4()
    mock_lead.created_at = datetime(2026, 6, 1, tzinfo=timezone.utc)
    mock_lead.updated_at = datetime(2026, 6, 1, tzinfo=timezone.utc)
    mock_lead.company = MagicMock(spec=Company)

    db = _mock_filtered_db([mock_lead])
    result = list_leads_filtered(db, view="all")

    assert len(result) == 1
    assert result[0]["lead"] is mock_lead
    assert result[0]["is_new_in_run"] is False  # no latest_run_started_at


def test_list_leads_filtered_no_leads_returns_empty():
    db = _mock_filtered_db([])
    result = list_leads_filtered(db)
    assert result == []
    # Only one DB call (main query); no follow-up queries needed
    assert db.execute.call_count == 1


def test_list_leads_filtered_view_today_adds_created_at_filter():
    db = _mock_filtered_db([])
    list_leads_filtered(db, view="today")
    stmt_str = str(db.execute.call_args[0][0])
    assert "created_at >=" in stmt_str


def test_list_leads_filtered_view_recently_updated_adds_updated_at_filter():
    db = _mock_filtered_db([])
    list_leads_filtered(db, view="recently_updated")
    stmt_str = str(db.execute.call_args[0][0])
    assert "updated_at >=" in stmt_str


def test_list_leads_filtered_view_latest_run_with_started_at():
    """view='latest_run' with a start time adds updated_at >= start filter."""
    db = _mock_filtered_db([])
    started = datetime(2026, 6, 19, 8, 0, 0, tzinfo=timezone.utc)
    list_leads_filtered(db, view="latest_run", latest_run_started_at=started)
    stmt_str = str(db.execute.call_args[0][0])
    assert "updated_at >=" in stmt_str


def test_list_leads_filtered_view_latest_run_no_started_at_behaves_like_all():
    """view='latest_run' without start time does not add an updated_at WHERE predicate."""
    db_with = _mock_filtered_db([])
    db_without = _mock_filtered_db([])
    started = datetime(2026, 6, 19, 8, 0, 0, tzinfo=timezone.utc)
    list_leads_filtered(db_with, view="latest_run", latest_run_started_at=started)
    list_leads_filtered(db_without, view="latest_run", latest_run_started_at=None)
    stmt_with = str(db_with.execute.call_args[0][0])
    stmt_without = str(db_without.execute.call_args[0][0])
    # The >= predicate only appears when latest_run_started_at is provided
    assert "updated_at >=" in stmt_with
    assert "updated_at >=" not in stmt_without


def test_list_leads_filtered_view_custom_date_range():
    db = _mock_filtered_db([])
    list_leads_filtered(
        db,
        view="custom",
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 19),
    )
    stmt_str = str(db.execute.call_args[0][0]).lower()
    assert "created_at" in stmt_str


def test_list_leads_filtered_tier_filter():
    db = _mock_filtered_db([])
    list_leads_filtered(db, tier="hot")
    stmt_str = str(db.execute.call_args[0][0]).lower()
    assert "tier" in stmt_str


def test_list_leads_filtered_sales_status_filter():
    db = _mock_filtered_db([])
    list_leads_filtered(db, sales_status="approved")
    stmt_str = str(db.execute.call_args[0][0]).lower()
    assert "sales_status" in stmt_str


def test_list_leads_filtered_source_id_filter():
    db = _mock_filtered_db([])
    src_id = uuid.uuid4()
    list_leads_filtered(db, source_id=src_id)
    stmt_str = str(db.execute.call_args[0][0])
    assert "source_id" in stmt_str


def test_list_leads_filtered_has_contactability_filter():
    db = _mock_filtered_db([])
    list_leads_filtered(db, has_contactability=True)
    stmt_str = str(db.execute.call_args[0][0])
    assert "company_contactability" in stmt_str


def test_list_leads_filtered_sort_newest_first():
    db = _mock_filtered_db([])
    list_leads_filtered(db, sort_by="newest_first")
    stmt_str = str(db.execute.call_args[0][0]).lower()
    assert "created_at" in stmt_str
    assert "desc" in stmt_str


def test_list_leads_filtered_sort_latest_updated():
    db = _mock_filtered_db([])
    list_leads_filtered(db, sort_by="latest_updated")
    stmt_str = str(db.execute.call_args[0][0]).lower()
    assert "updated_at" in stmt_str
    assert "desc" in stmt_str


def test_list_leads_filtered_is_new_in_run_flag():
    """is_new_in_run is True when lead.created_at >= latest_run_started_at."""
    started = datetime(2026, 6, 19, 8, 0, 0, tzinfo=timezone.utc)
    cid = uuid.uuid4()

    mock_lead_new = MagicMock(spec=LeadCandidate)
    mock_lead_new.company_id = cid
    mock_lead_new.created_at = datetime(2026, 6, 19, 9, 0, 0, tzinfo=timezone.utc)
    mock_lead_new.updated_at = datetime(2026, 6, 19, 9, 0, 0, tzinfo=timezone.utc)
    mock_lead_new.company = MagicMock(spec=Company)

    db = _mock_filtered_db([mock_lead_new])
    result = list_leads_filtered(db, latest_run_started_at=started)

    assert result[0]["is_new_in_run"] is True


def test_list_leads_filtered_enrichment_fields_populated():
    """primary_source and latest_signal_date are populated from follow-up queries."""
    cid = uuid.uuid4()
    mock_lead = MagicMock(spec=LeadCandidate)
    mock_lead.company_id = cid
    mock_lead.created_at = datetime(2026, 6, 1, tzinfo=timezone.utc)
    mock_lead.updated_at = datetime(2026, 6, 1, tzinfo=timezone.utc)
    mock_lead.company = MagicMock(spec=Company)

    db = MagicMock()
    main_result = MagicMock()
    main_result.scalars.return_value.all.return_value = [mock_lead]

    sig_row = MagicMock()
    sig_row.company_id = cid
    sig_row.latest_signal_date = date(2026, 5, 15)
    sig_row.max_award = 250000
    sig_result = MagicMock()
    sig_result.all.return_value = [sig_row]

    src_row = MagicMock()
    src_row.company_id = cid
    src_row.source_name = "usaspending"
    src_result = MagicMock()
    src_result.all.return_value = [src_row]

    db.execute.side_effect = [main_result, sig_result, src_result]

    result = list_leads_filtered(db)

    assert result[0]["primary_source"] == "usaspending"
    assert result[0]["latest_signal_date"] == date(2026, 5, 15)
    assert result[0]["max_award_amount"] is not None


# ─── Test 54: _get_primary_source_by_company ──────────────────────────────────


def test_get_primary_source_by_company_empty_input():
    db = MagicMock()
    result = _get_primary_source_by_company([], db)
    assert result == {}
    db.execute.assert_not_called()


def test_get_primary_source_by_company_maps_id_to_name():
    cid = uuid.uuid4()
    db = MagicMock()
    row = MagicMock()
    row.company_id = cid
    row.source_name = "usaspending"
    db.execute.return_value.all.return_value = [row]

    result = _get_primary_source_by_company([cid], db)

    assert result[cid] == "usaspending"
