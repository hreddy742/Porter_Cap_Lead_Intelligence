"""
Tests for app/processing/gates.py.

All tests use MagicMock sessions — no real database required.

Execute call order inside evaluate_mandatory_gates:
  call 0 → EvidenceItem query
  call 1 → Signal query
  call 2 → CompanyIdentifier query  (inside check_suppression)
  call 3 → SuppressionList query    (inside check_suppression)
  call 4 → LeadCandidate query      (duplicate active check)

db.get(Company, id) is called by both evaluate_mandatory_gates and (if reached)
check_suppression internally; both must return the same company.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from app.db.models import (
    Company,
    CompanyIdentifier,
    EvidenceItem,
    LeadCandidate,
    Signal,
    SuppressionList,
)
from app.processing.gates import _parse_env_decimal, evaluate_mandatory_gates, flag_excluded_sector


# ─── Shared mock builders ─────────────────────────────────────────────────────


def _make_company(
    *,
    country: str = "US",
    naics_code: str | None = None,
    industry: str | None = None,
    business_type: str | None = None,
) -> MagicMock:
    c = MagicMock(spec=Company)
    c.id = uuid.uuid4()
    c.canonical_name = "Acme Federal Services LLC"
    c.state = "TX"
    c.country = country
    c.naics_code = naics_code
    c.industry = industry
    c.business_type = business_type
    return c


def _make_evidence() -> MagicMock:
    return MagicMock(spec=EvidenceItem)


def _make_signal(
    freshness_score: float,
    award_amount=None,
    signal_date=None,
) -> MagicMock:
    s = MagicMock(spec=Signal)
    s.freshness_score = freshness_score
    s.award_amount = award_amount
    s.signal_date = signal_date
    return s


def _make_identifier(*, company_id, id_type: str, id_value: str) -> MagicMock:
    ident = MagicMock(spec=CompanyIdentifier)
    ident.company_id = company_id
    ident.id_type = id_type
    ident.id_value = id_value
    return ident


def _make_suppression_entry(
    *, match_type: str, match_value: str, reason: str
) -> MagicMock:
    e = MagicMock(spec=SuppressionList)
    e.id = uuid.uuid4()
    e.match_type = match_type
    e.match_value = match_value
    e.reason = reason
    e.active = True
    return e


def _make_active_lead(company_id) -> MagicMock:
    lc = MagicMock(spec=LeadCandidate)
    lc.id = uuid.uuid4()
    lc.company_id = company_id
    lc.status = "active"
    lc.deleted_at = None
    return lc


def _make_session(
    *,
    company: MagicMock | None = None,
    evidence_items: list | None = None,
    signals: list | None = None,
    identifiers: list | None = None,
    suppression_entries: list | None = None,
    active_leads: list | None = None,
) -> MagicMock:
    """
    Build a mocked SQLAlchemy Session for gate tests.

    execute() call order (mirrors the implementation):
      0 → EvidenceItem rows
      1 → Signal rows
      2 → CompanyIdentifier rows   (inside check_suppression)
      3 → SuppressionList rows     (inside check_suppression)
      4 → LeadCandidate rows       (duplicate active check)

    db.get(Company, pk) always returns `company` regardless of how many
    times it is called (both gates and check_suppression call it).
    """
    ordered_results = [
        evidence_items or [],
        signals or [],
        identifiers or [],
        suppression_entries or [],
        active_leads or [],
    ]
    call_count = [0]

    def _execute(stmt):
        result = MagicMock()
        n = call_count[0]
        call_count[0] += 1
        result.scalars.return_value.all.return_value = ordered_results[n]
        return result

    def _get(cls, pk):
        if cls is Company:
            return company
        return None

    s = MagicMock()
    s.get.side_effect = _get
    s.execute.side_effect = _execute
    return s


# ─── Convenience: fresh signal + evidence (pass the first two gates) ──────────

_EVIDENCE = [_make_evidence()]
# Award amount qualifies (> $10K, within 90 days) so Gate 10 does not fire
# for tests that rely on _FRESH_SIGNAL and reach the final gate.
_FRESH_SIGNAL = [_make_signal(0.5, award_amount=Decimal("50000"), signal_date=date.today())]
_UEI = "TESTCOMPANY_UEI_001"


# ─── Test 1: no evidence → no_evidence gate ───────────────────────────────────


def test_no_evidence_returns_no_evidence_gate():
    """Company with no evidence_items is archived with gate_reason='no_evidence'."""
    company = _make_company()
    db = _make_session(company=company, evidence_items=[])

    result = evaluate_mandatory_gates(company.id, db)

    assert result["passed"] is False
    assert result["gate_reason"] == "no_evidence"
    assert result["route"] == "archive"
    assert result["should_score"] is False


# ─── Test 2: has evidence, no signal → no_signal gate ────────────────────────


def test_no_signal_returns_no_signal_gate():
    """Company with evidence but no signals is archived with gate_reason='no_signal'."""
    company = _make_company()
    db = _make_session(company=company, evidence_items=_EVIDENCE, signals=[])

    result = evaluate_mandatory_gates(company.id, db)

    assert result["passed"] is False
    assert result["gate_reason"] == "no_signal"
    assert result["route"] == "archive"
    assert result["should_score"] is False


# ─── Test 3: only stale signals → stale_signal gate ──────────────────────────


def test_all_stale_signals_returns_stale_signal_gate():
    """Company whose every signal has freshness_score < 0.1 is archived as stale."""
    company = _make_company()
    stale_signals = [_make_signal(0.05), _make_signal(0.0)]
    db = _make_session(company=company, evidence_items=_EVIDENCE, signals=stale_signals)

    result = evaluate_mandatory_gates(company.id, db)

    assert result["passed"] is False
    assert result["gate_reason"] == "stale_signal"
    assert result["route"] == "archive"
    assert result["should_score"] is False


# ─── Test 4: excluded industry → excluded_industry gate ──────────────────────


def test_excluded_industry_returns_excluded_industry_gate():
    """Company with industry keyword 'banking' is archived as excluded_industry."""
    company = _make_company(industry="banking services")
    db = _make_session(company=company, evidence_items=_EVIDENCE, signals=_FRESH_SIGNAL)

    result = evaluate_mandatory_gates(company.id, db)

    assert result["passed"] is False
    assert result["gate_reason"] == "excluded_industry"
    assert result["route"] == "archive"
    assert result["should_score"] is False


def test_naics_52x_no_longer_hard_blocked():
    """NAICS 52x (Finance) is no longer a hard block — company passes Gate 4.
    Design decision June 24 2026: all sector filtering is soft-flag only;
    humans and AI decide. The lead is still scored and flagged via flag_excluded_sector().
    """
    company = _make_company(naics_code="522110")  # Commercial Banking
    db = _make_session(company=company, evidence_items=_EVIDENCE, signals=_FRESH_SIGNAL)

    result = evaluate_mandatory_gates(company.id, db)

    assert result["passed"] is True
    assert result["should_score"] is True


# ─── Test 5: non-US company → non_us gate ────────────────────────────────────


def test_non_us_company_returns_non_us_gate():
    """Company with country != 'US' is archived with gate_reason='non_us'."""
    company = _make_company(country="CA")
    db = _make_session(company=company, evidence_items=_EVIDENCE, signals=_FRESH_SIGNAL)

    result = evaluate_mandatory_gates(company.id, db)

    assert result["passed"] is False
    assert result["gate_reason"] == "non_us"
    assert result["route"] == "archive"
    assert result["should_score"] is False


# ─── Test 6: B2C company → b2c gate ──────────────────────────────────────────


def test_b2c_company_returns_b2c_gate():
    """Company with business_type='b2c' is archived with gate_reason='b2c'."""
    company = _make_company(business_type="b2c")
    db = _make_session(company=company, evidence_items=_EVIDENCE, signals=_FRESH_SIGNAL)

    result = evaluate_mandatory_gates(company.id, db)

    assert result["passed"] is False
    assert result["gate_reason"] == "b2c"
    assert result["route"] == "archive"
    assert result["should_score"] is False


# ─── Test 7: do_not_contact → hard_block ─────────────────────────────────────


def test_do_not_contact_suppression_returns_hard_block():
    """do_not_contact suppression reason routes to hard_block and blocks scoring."""
    company = _make_company()
    ident = _make_identifier(company_id=company.id, id_type="uei", id_value=_UEI)
    supp = _make_suppression_entry(
        match_type="uei", match_value=_UEI, reason="do_not_contact"
    )
    db = _make_session(
        company=company,
        evidence_items=_EVIDENCE,
        signals=_FRESH_SIGNAL,
        identifiers=[ident],
        suppression_entries=[supp],
    )

    result = evaluate_mandatory_gates(company.id, db)

    assert result["passed"] is False
    assert result["should_score"] is False
    assert result["route"] == "hard_block"
    assert result["gate_reason"] == "do_not_contact"
    assert result["suppression"] is not None
    assert result["suppression"]["suppressed"] is True


# ─── Test 8: compliance_blocked → hard_block ─────────────────────────────────


def test_compliance_blocked_suppression_returns_hard_block():
    """compliance_blocked suppression reason also routes to hard_block."""
    company = _make_company()
    ident = _make_identifier(company_id=company.id, id_type="uei", id_value=_UEI)
    supp = _make_suppression_entry(
        match_type="uei", match_value=_UEI, reason="compliance_blocked"
    )
    db = _make_session(
        company=company,
        evidence_items=_EVIDENCE,
        signals=_FRESH_SIGNAL,
        identifiers=[ident],
        suppression_entries=[supp],
    )

    result = evaluate_mandatory_gates(company.id, db)

    assert result["passed"] is False
    assert result["should_score"] is False
    assert result["route"] == "hard_block"
    assert result["gate_reason"] == "compliance_blocked"


# ─── Test 9: existing_customer → account_review ──────────────────────────────


def test_existing_customer_suppression_returns_account_review():
    """existing_customer routes to account_review — not hard_block, not archive."""
    company = _make_company()
    ident = _make_identifier(company_id=company.id, id_type="uei", id_value=_UEI)
    supp = _make_suppression_entry(
        match_type="uei", match_value=_UEI, reason="existing_customer"
    )
    db = _make_session(
        company=company,
        evidence_items=_EVIDENCE,
        signals=_FRESH_SIGNAL,
        identifiers=[ident],
        suppression_entries=[supp],
    )

    result = evaluate_mandatory_gates(company.id, db)

    assert result["passed"] is False
    assert result["should_score"] is False
    assert result["route"] == "account_review"
    assert result["gate_name"] == "suppression_route"
    assert result["gate_reason"] == "existing_customer"


# ─── Test 10: sf_lead_id match → existing_lead_review ────────────────────────


def test_sf_lead_id_suppression_returns_existing_lead_review():
    """A company matched by sf_lead_id routes to existing_lead_review."""
    company = _make_company()
    sf_id = "00Q5000001SFLEAD"
    ident = _make_identifier(company_id=company.id, id_type="sf_lead_id", id_value=sf_id)
    supp = _make_suppression_entry(
        match_type="sf_lead_id", match_value=sf_id, reason="existing_lead"
    )
    db = _make_session(
        company=company,
        evidence_items=_EVIDENCE,
        signals=_FRESH_SIGNAL,
        identifiers=[ident],
        suppression_entries=[supp],
    )

    result = evaluate_mandatory_gates(company.id, db)

    assert result["passed"] is False
    assert result["should_score"] is False
    assert result["route"] == "existing_lead_review"
    assert result["gate_name"] == "suppression_route"


# ─── Test 11: duplicate suppression → duplicate_review ───────────────────────


def test_duplicate_suppression_returns_duplicate_review():
    """duplicate suppression reason routes to duplicate_review."""
    company = _make_company()
    ident = _make_identifier(company_id=company.id, id_type="uei", id_value=_UEI)
    supp = _make_suppression_entry(
        match_type="uei", match_value=_UEI, reason="duplicate"
    )
    db = _make_session(
        company=company,
        evidence_items=_EVIDENCE,
        signals=_FRESH_SIGNAL,
        identifiers=[ident],
        suppression_entries=[supp],
    )

    result = evaluate_mandatory_gates(company.id, db)

    assert result["passed"] is False
    assert result["should_score"] is False
    assert result["route"] == "duplicate_review"
    assert result["gate_name"] == "suppression_route"
    assert result["gate_reason"] == "duplicate"


# ─── Test 12: clean company passes all gates ──────────────────────────────────


def test_clean_company_passes_all_gates():
    """A company with evidence, fresh signal, no suppression, no active lead passes."""
    company = _make_company()
    db = _make_session(
        company=company,
        evidence_items=_EVIDENCE,
        signals=_FRESH_SIGNAL,
        identifiers=[],
        suppression_entries=[],
        active_leads=[],
    )

    result = evaluate_mandatory_gates(company.id, db)

    assert result["passed"] is True
    assert result["gate_name"] is None
    assert result["gate_reason"] is None
    assert result["route"] == "score"
    assert result["should_score"] is True
    assert result["suppression"] is not None
    assert result["suppression"]["suppressed"] is False


# ─── Test 13: duplicate active lead candidate ─────────────────────────────────


def test_duplicate_active_lead_returns_duplicate_active_gate():
    """A company that already has an active lead_candidate is gated as duplicate_active."""
    company = _make_company()
    active_lead = _make_active_lead(company.id)
    db = _make_session(
        company=company,
        evidence_items=_EVIDENCE,
        signals=_FRESH_SIGNAL,
        identifiers=[],
        suppression_entries=[],
        active_leads=[active_lead],
    )

    result = evaluate_mandatory_gates(company.id, db)

    assert result["passed"] is False
    assert result["gate_reason"] == "duplicate_active"
    assert result["route"] == "existing_lead_review"
    assert result["should_score"] is False


# ─── Test 14: gate order is respected ────────────────────────────────────────


def test_gate_order_no_evidence_fires_before_no_signal():
    """When a company has neither evidence nor signals, no_evidence fires first (gate 1 < gate 2)."""
    company = _make_company()
    # Both gates would fire, but gate 1 must win.
    db = _make_session(company=company, evidence_items=[], signals=[])

    result = evaluate_mandatory_gates(company.id, db)

    assert result["gate_reason"] == "no_evidence", (
        "Gate 1 (no_evidence) must fire before Gate 2 (no_signal)"
    )
    assert result["gate_reason"] != "no_signal"


# ─── Gate 10 helpers ─────────────────────────────────────────────────────────


def _days_ago(n: int) -> date:
    return date.today() - timedelta(days=n)


def _make_gate10_db(signals_list: list):
    """Return (company, db) with gates 1-9 all passing; Gate 10 depends on signals_list."""
    company = _make_company()
    db = _make_session(
        company=company,
        evidence_items=_EVIDENCE,
        signals=signals_list,
        identifiers=[],
        suppression_entries=[],
        active_leads=[],
    )
    return company, db


# ─── Gate 10: award amount quality gate ──────────────────────────────────────


def test_negative_award_gated_as_too_small():
    """A single negative award provides no qualification — company is gated."""
    sig = _make_signal(0.5, award_amount=Decimal("-500"), signal_date=date.today())
    company, db = _make_gate10_db([sig])

    result = evaluate_mandatory_gates(company.id, db)

    assert result["passed"] is False
    assert result["gate_reason"] == "award_amount_too_small"
    assert result["route"] == "archive"
    assert result["should_score"] is False


def test_zero_award_gated_as_too_small():
    """A single zero award provides no qualification — company is gated."""
    sig = _make_signal(0.5, award_amount=Decimal("0"), signal_date=date.today())
    company, db = _make_gate10_db([sig])

    result = evaluate_mandatory_gates(company.id, db)

    assert result["passed"] is False
    assert result["gate_reason"] == "award_amount_too_small"
    assert result["route"] == "archive"


def test_single_tiny_award_below_threshold_gated():
    """$50.50 single award — both single and 90-day thresholds fail — gated."""
    sig = _make_signal(0.5, award_amount=Decimal("50.50"), signal_date=date.today())
    company, db = _make_gate10_db([sig])

    result = evaluate_mandatory_gates(company.id, db)

    assert result["passed"] is False
    assert result["gate_reason"] == "award_amount_too_small"


def test_multiple_small_awards_totaling_above_threshold_pass():
    """Ten × $1,500 within 90 days totals $15,000 — 90-day threshold met — not gated."""
    sigs = [
        _make_signal(0.5, award_amount=Decimal("1500"), signal_date=date.today())
        for _ in range(10)
    ]
    company, db = _make_gate10_db(sigs)

    result = evaluate_mandatory_gates(company.id, db)

    assert result["passed"] is True
    assert result["route"] == "score"


def test_single_award_above_threshold_passes():
    """One $12,000 award exceeds the single-award threshold — not gated."""
    sig = _make_signal(0.5, award_amount=Decimal("12000"), signal_date=date.today())
    company, db = _make_gate10_db([sig])

    result = evaluate_mandatory_gates(company.id, db)

    assert result["passed"] is True
    assert result["route"] == "score"


def test_boundary_single_9999_99_gated():
    """$9,999.99 single award with no other awards — both thresholds fail — gated."""
    sig = _make_signal(0.5, award_amount=Decimal("9999.99"), signal_date=date.today())
    company, db = _make_gate10_db([sig])

    result = evaluate_mandatory_gates(company.id, db)

    assert result["passed"] is False
    assert result["gate_reason"] == "award_amount_too_small"


def test_boundary_single_10000_passes():
    """$10,000 exactly meets the single-award threshold — passes."""
    sig = _make_signal(0.5, award_amount=Decimal("10000"), signal_date=date.today())
    company, db = _make_gate10_db([sig])

    result = evaluate_mandatory_gates(company.id, db)

    assert result["passed"] is True


def test_boundary_90d_aggregate_below_threshold_gated():
    """Ten × $999.99 in 90 days = $9,999.90 — below 90-day threshold, no single qualifier — gated."""
    sigs = [
        _make_signal(0.5, award_amount=Decimal("999.99"), signal_date=date.today())
        for _ in range(10)
    ]
    company, db = _make_gate10_db(sigs)

    result = evaluate_mandatory_gates(company.id, db)

    assert result["passed"] is False
    assert result["gate_reason"] == "award_amount_too_small"


def test_boundary_90d_aggregate_10000_passes():
    """Ten × $1,000 in 90 days = $10,000 — meets the 90-day threshold — passes."""
    sigs = [
        _make_signal(0.5, award_amount=Decimal("1000"), signal_date=date.today())
        for _ in range(10)
    ]
    company, db = _make_gate10_db(sigs)

    result = evaluate_mandatory_gates(company.id, db)

    assert result["passed"] is True


def test_old_awards_excluded_from_90d_window():
    """Many small awards older than 90 days — excluded from 90-day total.
    Without a qualifying single award, the company is gated."""
    sigs = [
        _make_signal(0.5, award_amount=Decimal("1500"), signal_date=_days_ago(91))
        for _ in range(20)
    ]
    company, db = _make_gate10_db(sigs)

    result = evaluate_mandatory_gates(company.id, db)

    assert result["passed"] is False
    assert result["gate_reason"] == "award_amount_too_small"


def test_mixed_negative_zero_positive_at_threshold_passes():
    """Negative + zero + two $5,000 positives — only positives count; 90-day total $10,000 — passes."""
    sigs = [
        _make_signal(0.5, award_amount=Decimal("-1000"), signal_date=date.today()),
        _make_signal(0.5, award_amount=Decimal("0"),     signal_date=date.today()),
        _make_signal(0.5, award_amount=Decimal("5000"),  signal_date=date.today()),
        _make_signal(0.5, award_amount=Decimal("5000"),  signal_date=date.today()),
    ]
    company, db = _make_gate10_db(sigs)

    result = evaluate_mandatory_gates(company.id, db)

    assert result["passed"] is True


# ─── _parse_env_decimal unit tests ───────────────────────────────────────────


def test_parse_env_decimal_unset_returns_default():
    """_parse_env_decimal returns the default when the env var is not set."""
    result = _parse_env_decimal("_GATE_TEST_VAR_UNSET_XYZ", Decimal("10000"))
    assert result == Decimal("10000")


def test_parse_env_decimal_valid_value_parsed():
    """_parse_env_decimal returns the parsed Decimal when the env var holds a valid number."""
    with patch.dict("os.environ", {"_GATE_TEST_VAR_XYZ": "25000"}):
        result = _parse_env_decimal("_GATE_TEST_VAR_XYZ", Decimal("10000"))
    assert result == Decimal("25000")


def test_parse_env_decimal_invalid_raises():
    """_parse_env_decimal raises ValueError with the variable name when the value is unparseable."""
    with patch.dict("os.environ", {"_GATE_TEST_VAR_XYZ": "not_a_number"}):
        with pytest.raises(ValueError, match="_GATE_TEST_VAR_XYZ"):
            _parse_env_decimal("_GATE_TEST_VAR_XYZ", Decimal("10000"))


# ─── flag_excluded_sector tests ───────────────────────────────────────────────


def _make_lead_candidate() -> MagicMock:
    lc = MagicMock(spec=LeadCandidate)
    lc.id = uuid.uuid4()
    lc.sector_excluded = False
    lc.sector_excluded_reason = None
    return lc


def _make_db_for_flag() -> MagicMock:
    db = MagicMock()
    db.flush.return_value = None
    return db


def test_flag_excluded_sector_healthcare_naics_flagged():
    """Company with NAICS 621100 (Health Care) → sector_excluded=True."""
    company = _make_company(naics_code="621100")
    lead = _make_lead_candidate()
    db = _make_db_for_flag()

    result = flag_excluded_sector(company, lead, db)

    assert result is True
    assert lead.sector_excluded is True
    assert lead.sector_excluded_reason is not None
    assert "62" in lead.sector_excluded_reason
    db.flush.assert_called_once()


def test_flag_excluded_sector_agriculture_naics_flagged():
    """Company with NAICS 111110 (Agriculture) → sector_excluded=True."""
    company = _make_company(naics_code="111110")
    lead = _make_lead_candidate()
    db = _make_db_for_flag()

    result = flag_excluded_sector(company, lead, db)

    assert result is True
    assert lead.sector_excluded is True
    assert "11" in lead.sector_excluded_reason


def test_flag_excluded_sector_public_admin_naics_flagged():
    """Company with NAICS 921100 (Public Administration) → sector_excluded=True."""
    company = _make_company(naics_code="921100")
    lead = _make_lead_candidate()
    db = _make_db_for_flag()

    result = flag_excluded_sector(company, lead, db)

    assert result is True
    assert lead.sector_excluded is True
    assert "92" in lead.sector_excluded_reason


def test_flag_excluded_sector_finance_naics_flagged():
    """Company with NAICS 521000 (Finance) → sector_excluded=True."""
    company = _make_company(naics_code="521000")
    lead = _make_lead_candidate()
    db = _make_db_for_flag()

    result = flag_excluded_sector(company, lead, db)

    assert result is True
    assert lead.sector_excluded is True
    assert "52" in lead.sector_excluded_reason


def test_flag_excluded_sector_professional_services_not_flagged():
    """Company with NAICS 541300 (Professional Services) → sector_excluded=False."""
    company = _make_company(naics_code="541300")
    lead = _make_lead_candidate()
    db = _make_db_for_flag()

    result = flag_excluded_sector(company, lead, db)

    assert result is False
    assert lead.sector_excluded is False
    db.flush.assert_not_called()


def test_flag_excluded_sector_none_naics_not_flagged():
    """Company with naics_code=None → sector_excluded unchanged, returns False."""
    company = _make_company(naics_code=None)
    lead = _make_lead_candidate()
    db = _make_db_for_flag()

    result = flag_excluded_sector(company, lead, db)

    assert result is False
    assert lead.sector_excluded is False
    db.flush.assert_not_called()
