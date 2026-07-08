"""
Tests for the SBA 7(a) loan connector and pipeline integration.

All connector tests use mocked file I/O — no real network calls, no database.
The session is a MagicMock configured per-test for dedup behaviour.

Coverage:
  Connector — filtering (NAICS, state, amount, date, LoanStatus)
  Connector — Pydantic validation (SBALoanRecord)
  Connector — deduplication (content hash)
  Evidence  — SBA payload dispatch and field extraction
  Signals   — SBA claim types handled, strength overrides
  Scoring   — SBA_LOAN_PIF +8, SBA_LOAN_ACTIVE +4 why_now bonus
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

import httpx

from app.pipeline.connectors.sba_loans import (
    SBALoanRecord,
    SBALoansConnector,
    _is_included_naics,
    _is_excluded_naics,
    _has_noise_keyword,
    _signal_type_for_status,
    _is_active_loan,
    _ensure_cache,
    _SBA_INCLUDED_NAICS_PREFIXES,
    _SBA_EXCLUDED_NAICS_PREFIXES,
    _SBA_NOISE_KEYWORDS,
    _MIN_LOAN_AMOUNT,
    _MIN_APPROVAL_DATE,
    _SKIP_LOAN_STATUSES,
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


def _valid_row(**overrides) -> dict:
    """Minimal valid SBA CSV row that passes all filters.

    Uses lowercase column names matching the current SBA FOIA CSV format.
    Loan status 'P I F' (with spaces) is the new format for paid-in-full.
    """
    base = {
        "borrname": "Acme Manufacturing LLC",
        "borrstreet": "123 Main St",
        "borrcity": "Birmingham",
        "borrstate": "AL",
        "borrzip": "35201",
        "naicscode": "332312",
        "naicsdescription": "Fabricated Structural Metal Manufacturing",
        "grossapproval": "500000",
        "approvaldate": "2023-06-15",
        "loanstatus": "P I F",
        "jobssupported": "25",
    }
    base.update(overrides)
    return base


def _run_connector(
    rows: list[dict],
    *,
    session: MagicMock | None = None,
    source: MagicMock | None = None,
    source_run: MagicMock | None = None,
    env: dict[str, str] | None = None,
    record_exists: bool = False,
) -> tuple[MagicMock, MagicMock]:
    """Run the connector with a mocked CSV and return (source_run, session)."""
    if session is None:
        session = _make_session(record_exists=record_exists)
    if source is None:
        source = _make_source()
    if source_run is None:
        source_run = _make_source_run()

    import csv
    import io

    if rows:
        fieldnames = list(rows[0].keys())
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
        csv_content = buf.getvalue()
    else:
        csv_content = "borrname,borrstate,grossapproval,approvaldate,loanstatus\n"

    env_patch = {"SBA_LOANS_TEST_LIMIT": "0", "SBA_CACHE_PATH": "/fake/cache.csv"}
    if env:
        env_patch.update(env)

    mock_path = MagicMock()
    mock_path.open.return_value.__enter__.return_value = io.StringIO(csv_content)

    with patch("app.pipeline.connectors.sba_loans._ensure_cache", return_value=mock_path):
        with patch.dict("os.environ", env_patch):
            connector = SBALoansConnector(session, source_run, source)
            connector.run()

    return source_run, session


# ─── NAICS filtering (soft-flag, not hard block) ──────────────────────────────


def test_restaurant_naics_soft_flagged():
    """NAICS 722511 (Restaurants) is still collected but soft-flagged sector_excluded."""
    source_run, session = _run_connector([_valid_row(naicscode="722511", naicsdescription="Restaurants")])
    assert source_run.records_valid == 1
    assert source_run.records_skipped == 0
    added = session.add.call_args[0][0]
    assert added.payload["sector_excluded"] is True


def test_hotel_naics_soft_flagged():
    """NAICS 721110 (Hotels) is still collected but soft-flagged sector_excluded."""
    source_run, session = _run_connector([_valid_row(naicscode="721110", naicsdescription="Hotels")])
    assert source_run.records_valid == 1
    added = session.add.call_args[0][0]
    assert added.payload["sector_excluded"] is True


def test_retail_naics_soft_flagged():
    """NAICS 441110 (New Car Dealers) is still collected but soft-flagged (Retail Trade 44)."""
    source_run, session = _run_connector([_valid_row(naicscode="441110")])
    assert source_run.records_valid == 1
    added = session.add.call_args[0][0]
    assert added.payload["sector_excluded"] is True


def test_manufacturing_naics_included_not_flagged():
    """NAICS 332312 (Manufacturing) must be included — B2B sector 33, not soft-flagged."""
    source_run, session = _run_connector([_valid_row(naicscode="332312")])
    assert source_run.records_valid == 1
    assert source_run.records_skipped == 0
    added = session.add.call_args[0][0]
    assert added.payload["sector_excluded"] is False


def test_staffing_naics_included():
    """NAICS 561320 (Staffing) must be included — B2B sector 56."""
    source_run, _ = _run_connector([_valid_row(naicscode="561320")])
    assert source_run.records_valid == 1


def test_wholesale_naics_included():
    """NAICS 423990 (Wholesale) must be included — B2B sector 42."""
    source_run, _ = _run_connector([_valid_row(naicscode="423990")])
    assert source_run.records_valid == 1


def test_medical_instruments_naics_included():
    """NAICS 339113 (Medical Instrument Manufacturing) must be included — B2B manufacturing."""
    source_run, _ = _run_connector([_valid_row(naicscode="339113")])
    assert source_run.records_valid == 1


def test_construction_naics_included():
    """NAICS 238210 (Electrical Contractors) must be included — Construction sector 23."""
    source_run, _ = _run_connector([_valid_row(naicscode="238210")])
    assert source_run.records_valid == 1


def test_blank_naics_included():
    """A row with no NAICS code at all must still be included (SAM.gov fills it later)."""
    source_run, session = _run_connector([_valid_row(naicscode="")])
    assert source_run.records_valid == 1
    added = session.add.call_args[0][0]
    assert added.payload["NaicsCode"] is None
    assert added.payload["sector_excluded"] is False


# ─── State filtering removed — all 50 states included ─────────────────────────


def test_state_ny_included():
    """State NY must now be included — Porter is expanding nationally."""
    source_run, _ = _run_connector([_valid_row(borrstate="NY")])
    assert source_run.records_valid == 1


def test_state_al_included():
    """State AL must be included."""
    source_run, _ = _run_connector([_valid_row(borrstate="AL")])
    assert source_run.records_valid == 1


def test_states_outside_old_icp_included():
    """States outside the old 7-state ICP list must now be included."""
    for state in ("CA", "NY", "WA", "OH", "IL"):
        source_run, _ = _run_connector([_valid_row(borrstate=state)])
        assert source_run.records_valid == 1, f"State {state} should be included"


# ─── Loan amount filtering ────────────────────────────────────────────────────


def test_amount_below_minimum_excluded():
    """Loan amount $30,000 must be excluded — below $50K minimum."""
    source_run, _ = _run_connector([_valid_row(grossapproval="30000")])
    assert source_run.records_skipped == 1
    assert source_run.records_valid == 0


def test_amount_above_5m_included():
    """
    Loan amount $6,000,000 must now be included — the $5M cap was removed
    (redundant with the SBA program's own $5M maximum).
    """
    source_run, _ = _run_connector([_valid_row(grossapproval="6000000")])
    assert source_run.records_valid == 1
    assert source_run.records_skipped == 0


def test_amount_500k_included():
    """Loan amount $500,000 must be included — above the $50K minimum."""
    source_run, _ = _run_connector([_valid_row(grossapproval="500000")])
    assert source_run.records_valid == 1


def test_amount_50k_boundary_included():
    """Loan amount exactly $50,000 must be included — at minimum boundary."""
    source_run, _ = _run_connector([_valid_row(grossapproval="50000")])
    assert source_run.records_valid == 1


def test_amount_5m_boundary_included():
    """Loan amount exactly $5,000,000 must be included."""
    source_run, _ = _run_connector([_valid_row(grossapproval="5000000")])
    assert source_run.records_valid == 1


# ─── LoanStatus filtering ─────────────────────────────────────────────────────


def test_chgoff_status_excluded():
    """LoanStatus CHGOFF (charged off/defaulted) must be excluded."""
    source_run, _ = _run_connector([_valid_row(loanstatus="CHGOFF")])
    assert source_run.records_skipped == 1
    assert source_run.records_valid == 0


def test_cancld_status_excluded():
    """LoanStatus CANCLD (cancelled) must be excluded."""
    source_run, _ = _run_connector([_valid_row(loanstatus="CANCLD")])
    assert source_run.records_skipped == 1
    assert source_run.records_valid == 0


def test_exempt_status_produces_sba_loan_active_signal():
    """
    LoanStatus EXEMPT means an active loan (not exempt from reporting) and
    must NOT be skipped — this was a critical bug fix per John Cox Miller,
    Porter Capital, July 2026.
    """
    source_run, session = _run_connector([_valid_row(loanstatus="EXEMPT")])
    assert source_run.records_valid == 1
    assert source_run.records_skipped == 0
    added = session.add.call_args[0][0]
    assert added.payload["sba_signal_type"] == "SBA_LOAN_ACTIVE"


def test_commit_status_produces_sba_loan_pending_signal():
    """LoanStatus COMMIT (just approved) must produce SBA_LOAN_PENDING."""
    source_run, session = _run_connector([_valid_row(loanstatus="COMMIT", approvaldate="2026-06-01")])
    assert source_run.records_valid == 1
    added = session.add.call_args[0][0]
    assert added.payload["sba_signal_type"] == "SBA_LOAN_PENDING"
    assert "needs working capital now" in added.payload["sba_status_note"].lower()


def test_pif_status_produces_sba_loan_pif_signal():
    """LoanStatus 'P I F' (new CSV format with spaces) must produce SBA_LOAN_PIF payload."""
    source_run, session = _run_connector([_valid_row(loanstatus="P I F")])
    assert source_run.records_valid == 1
    added = session.add.call_args[0][0]
    assert added.payload["sba_signal_type"] == "SBA_LOAN_PIF"


def test_pif_status_old_format_still_works():
    """LoanStatus 'PIF' (old format without spaces) must also produce SBA_LOAN_PIF."""
    source_run, session = _run_connector([_valid_row(loanstatus="PIF")])
    assert source_run.records_valid == 1
    added = session.add.call_args[0][0]
    assert added.payload["sba_signal_type"] == "SBA_LOAN_PIF"


def test_blank_loan_status_produces_sba_loan_active_signal():
    """Blank LoanStatus (active loan) must produce signal_type SBA_LOAN_ACTIVE."""
    source_run, session = _run_connector([_valid_row(loanstatus="")])
    assert source_run.records_valid == 1
    added = session.add.call_args[0][0]
    assert added.payload["sba_signal_type"] == "SBA_LOAN_ACTIVE"


# ─── Activeness-based date filtering ──────────────────────────────────────────


def test_approval_date_2015_excluded():
    """ApprovalDate in 2015 (blank status) must be excluded — outside the 7-year window."""
    source_run, _ = _run_connector([_valid_row(approvaldate="2015-06-15", loanstatus="")])
    assert source_run.records_skipped == 1
    assert source_run.records_valid == 0


def test_approval_date_2023_included():
    """ApprovalDate in 2023 must be included — within the 7-year window."""
    source_run, _ = _run_connector([_valid_row(approvaldate="2023-06-15")])
    assert source_run.records_valid == 1


def test_approval_date_2019_01_01_boundary_included():
    """ApprovalDate exactly 2019-01-01 must be included — at the 7-year cutoff boundary."""
    source_run, _ = _run_connector([_valid_row(approvaldate="2019-01-01", loanstatus="")])
    assert source_run.records_valid == 1


def test_exempt_status_included_regardless_of_age():
    """An EXEMPT loan from 2015 must still be included — active loans skip the recency check."""
    source_run, _ = _run_connector([_valid_row(approvaldate="2015-01-01", loanstatus="EXEMPT")])
    assert source_run.records_valid == 1
    assert source_run.records_skipped == 0


def test_commit_status_included_regardless_of_age():
    """A COMMIT loan must be included regardless of approval date age."""
    source_run, _ = _run_connector([_valid_row(approvaldate="2015-01-01", loanstatus="COMMIT")])
    assert source_run.records_valid == 1
    assert source_run.records_skipped == 0


def test_is_active_loan_exempt_always_true():
    assert _is_active_loan("EXEMPT", date(2010, 1, 1)) is True


def test_is_active_loan_commit_always_true():
    assert _is_active_loan("COMMIT", date(2010, 1, 1)) is True


def test_is_active_loan_pif_old_false():
    assert _is_active_loan("PIF", date(2015, 1, 1)) is False


def test_is_active_loan_pif_recent_true():
    assert _is_active_loan("PIF", date(2023, 1, 1)) is True


# ─── Deduplication ───────────────────────────────────────────────────────────


def test_same_company_twice_produces_one_record():
    """Same row twice in the same run: second is deduped via content hash."""
    row = _valid_row()
    first_run, session1 = _run_connector([row], record_exists=False)
    assert first_run.records_valid == 1
    assert first_run.records_skipped == 0

    second_run, session2 = _run_connector([row], record_exists=True)
    assert second_run.records_skipped == 1
    assert second_run.records_valid == 0


# ─── Evidence fields ─────────────────────────────────────────────────────────


def test_evidence_stores_sba_pif_true_for_pif_loan():
    """Evidence item extracted from PIF payload must have sba_pif=True."""
    from app.processing.evidence import extract_evidence
    from datetime import datetime, timezone

    evidence_id = uuid.uuid4()
    raw_event_id = uuid.uuid4()
    source_id = uuid.uuid4()

    payload = {
        "BorrName": "Test Corp LLC",
        "BorrState": "AL",
        "BorrCity": "Huntsville",
        "BorrStreet": "456 Oak Ave",
        "BorrZip": "35802",
        "NaicsCode": "332312",
        "NaicsDescription": "Fabricated Structural Metal Manufacturing",
        "GrossApproval": "750000",
        "ApprovalDate": "2023-06-01",
        "LoanStatus": "PIF",
        "JobsSupported": 10,
        "sba_signal_type": "SBA_LOAN_PIF",
        "sba_status_note": "Paid in full — proven financing need, now scaling",
        "description": "SBA 7(a) loan of $750,000 approved 2023-06-01. Status: PIF.",
    }

    raw_event = MagicMock()
    raw_event.id = raw_event_id
    raw_event.source_id = source_id
    raw_event.payload = payload
    raw_event.source_url = "https://data.sba.gov/dataset/7-a-504-foia"

    db = MagicMock()
    db.get.return_value = raw_event
    db.flush.return_value = None

    result = extract_evidence(raw_event_id, db)

    assert len(result) == 1
    evidence = result[0]
    assert evidence.claim_supported == "SBA_LOAN_PIF"
    assert evidence.extracted_fields["sba_pif"] is True
    assert evidence.extracted_fields["award_amount"] == "750000"
    assert evidence.extracted_fields["state_code"] == "AL"


def test_evidence_stores_loan_amount_correctly():
    """Evidence item must store GrossApproval as award_amount in extracted_fields."""
    from app.processing.evidence import extract_evidence

    payload = {
        "BorrName": "Wholesale Widgets Inc",
        "BorrState": "TX",
        "BorrCity": "Dallas",
        "BorrStreet": None,
        "BorrZip": "75201",
        "NaicsCode": "423990",
        "NaicsDescription": "Wholesale Trade",
        "GrossApproval": "250000",
        "ApprovalDate": "2024-03-10",
        "LoanStatus": "",
        "JobsSupported": None,
        "sba_signal_type": "SBA_LOAN_ACTIVE",
        "sba_status_note": "Active SBA loan",
        "description": "SBA 7(a) loan of $250,000.",
    }

    raw_event = MagicMock()
    raw_event.id = uuid.uuid4()
    raw_event.source_id = uuid.uuid4()
    raw_event.payload = payload
    raw_event.source_url = "https://data.sba.gov/dataset/7-a-504-foia"

    db = MagicMock()
    db.get.return_value = raw_event

    result = extract_evidence(raw_event.id, db)

    assert len(result) == 1
    assert result[0].extracted_fields["award_amount"] == "250000"
    assert result[0].claim_supported == "SBA_LOAN_ACTIVE"


def test_evidence_source_url_points_to_sba_gov():
    """Evidence source_url must reference data.sba.gov."""
    from app.processing.evidence import extract_evidence

    payload = {
        "BorrName": "Acme Staffing LLC",
        "BorrState": "GA",
        "BorrCity": "Atlanta",
        "BorrStreet": "789 Peach Blvd",
        "BorrZip": "30301",
        "NaicsCode": "561320",
        "NaicsDescription": "Temporary Help Services",
        "GrossApproval": "100000",
        "ApprovalDate": "2023-01-15",
        "LoanStatus": "PIF",
        "JobsSupported": 5,
        "sba_signal_type": "SBA_LOAN_PIF",
        "sba_status_note": "Paid in full",
        "description": "SBA 7(a) loan.",
    }

    raw_event = MagicMock()
    raw_event.id = uuid.uuid4()
    raw_event.source_id = uuid.uuid4()
    raw_event.payload = payload
    raw_event.source_url = "https://data.sba.gov/dataset/7-a-504-foia"

    db = MagicMock()
    db.get.return_value = raw_event

    result = extract_evidence(raw_event.id, db)
    assert "sba.gov" in result[0].source_url


# ─── Signal detection — SBA types handled ────────────────────────────────────


def test_sba_loan_pif_signal_type_is_handled():
    """detect_signals_for_evidence must create a signal for SBA_LOAN_PIF claim."""
    from app.processing.signals import detect_signals_for_evidence

    evidence_id = uuid.uuid4()
    company_id = uuid.uuid4()
    source_id = uuid.uuid4()

    evidence = MagicMock()
    evidence.id = evidence_id
    evidence.company_id = company_id
    evidence.source_id = source_id
    evidence.claim_supported = "SBA_LOAN_PIF"
    evidence.freshness_score = Decimal("0.40")
    evidence.extracted_fields = {
        "award_amount": "500000",
        "action_date": "2023-06-15",
    }

    db = MagicMock()
    db.get.return_value = evidence
    db.execute.return_value.scalars.return_value.all.return_value = []

    result = detect_signals_for_evidence(evidence_id, db)

    assert len(result) == 1
    signal = result[0]
    assert signal.signal_type == "SBA_LOAN_PIF"
    assert signal.signal_strength == "strong"


def test_sba_loan_active_signal_strength_is_medium():
    """SBA_LOAN_ACTIVE must produce signal_strength='medium', not derived from amount."""
    from app.processing.signals import detect_signals_for_evidence

    evidence_id = uuid.uuid4()
    company_id = uuid.uuid4()
    source_id = uuid.uuid4()

    evidence = MagicMock()
    evidence.id = evidence_id
    evidence.company_id = company_id
    evidence.source_id = source_id
    evidence.claim_supported = "SBA_LOAN_ACTIVE"
    evidence.freshness_score = Decimal("0.50")
    evidence.extracted_fields = {
        "award_amount": "75000",  # below $250K — would be "weak" by amount alone
        "action_date": "2023-03-20",
    }

    db = MagicMock()
    db.get.return_value = evidence
    db.execute.return_value.scalars.return_value.all.return_value = []

    result = detect_signals_for_evidence(evidence_id, db)

    assert len(result) == 1
    assert result[0].signal_strength == "medium"


# ─── Scoring — SBA why_now bonus ─────────────────────────────────────────────


def test_sba_loan_pif_why_now_bonus_is_8():
    """A company with an SBA_LOAN_PIF signal must receive +8 to why_now."""
    from app.processing.scoring import score_company
    from app.processing.gates import evaluate_mandatory_gates

    company_id = uuid.uuid4()
    scoring_config_id = uuid.uuid4()

    company = MagicMock()
    company.id = company_id
    company.naics_code = "332312"  # Manufacturing — also gets NAICS bonuses
    company.country = "US"
    company.industry = "manufacturing"
    company.business_type = None

    evidence_id = uuid.uuid4()
    evidence = MagicMock()
    evidence.id = evidence_id
    evidence.source_url = "https://data.sba.gov/dataset/7-a-504-foia"
    evidence.claim_supported = "SBA_LOAN_PIF"
    evidence.freshness_score = Decimal("0.85")
    evidence.extracted_fields = {
        "award_amount": "500000",
        "action_date": "2023-06-15",
    }

    signal_id = uuid.uuid4()
    signal = MagicMock()
    signal.id = signal_id
    signal.evidence_id = evidence_id
    signal.signal_type = "SBA_LOAN_PIF"
    signal.signal_strength = "strong"
    signal.freshness_score = Decimal("0.85")
    signal.award_amount = Decimal("500000")
    signal.signal_date = date(2023, 6, 15)

    config = MagicMock()
    config.id = scoring_config_id
    config.config_hash = "abc123"

    candidate = MagicMock()
    candidate.id = uuid.uuid4()

    db = MagicMock()
    db.get.return_value = company

    def execute_side_effect(query):
        result = MagicMock()
        query_str = str(query)
        if "evidence_items" in query_str.lower() or "EvidenceItem" in str(query):
            result.scalars.return_value.all.return_value = [evidence]
            result.scalars.return_value.first.return_value = None
        elif "signals" in query_str.lower() or "Signal" in str(query):
            result.scalars.return_value.all.return_value = [signal]
            result.scalars.return_value.first.return_value = None
        elif "scoring_configs" in query_str.lower() or "ScoringConfig" in str(query):
            result.scalars.return_value.first.return_value = config
        elif "lead_candidates" in query_str.lower() or "LeadCandidate" in str(query):
            result.scalars.return_value.first.return_value = None
        else:
            result.scalars.return_value.all.return_value = []
            result.scalars.return_value.first.return_value = None
        return result

    db.execute.side_effect = execute_side_effect

    with patch("app.processing.scoring.evaluate_mandatory_gates") as mock_gates:
        mock_gates.return_value = {
            "passed": True,
            "should_score": True,
            "gate_name": None,
            "gate_reason": None,
            "route": "score",
            "suppression": None,
        }
        with patch("app.processing.scoring.flag_excluded_sector"):
            result = score_company(company_id, db)

    assert result["scored"] is True
    why_now = result["component_breakdown"]["why_now"]["points"]
    # SBA_LOAN_PIF adds +8 bonus; base why_now from SBA freshness is 0 (no contract signals)
    assert why_now == 8, f"Expected why_now=8 (SBA PIF bonus), got {why_now}"


def test_sba_loan_active_why_now_bonus_is_4():
    """A company with an SBA_LOAN_ACTIVE signal must receive +4 to why_now."""
    from app.processing.scoring import score_company

    company_id = uuid.uuid4()
    scoring_config_id = uuid.uuid4()

    company = MagicMock()
    company.id = company_id
    company.naics_code = "561320"
    company.country = "US"
    company.industry = "staffing"
    company.business_type = None

    evidence_id = uuid.uuid4()
    evidence = MagicMock()
    evidence.id = evidence_id
    evidence.source_url = "https://data.sba.gov/dataset/7-a-504-foia"
    evidence.claim_supported = "SBA_LOAN_ACTIVE"
    evidence.freshness_score = Decimal("0.85")
    evidence.extracted_fields = {"award_amount": "100000", "action_date": "2024-01-10"}

    signal = MagicMock()
    signal.id = uuid.uuid4()
    signal.evidence_id = evidence_id
    signal.signal_type = "SBA_LOAN_ACTIVE"
    signal.signal_strength = "medium"
    signal.freshness_score = Decimal("0.85")
    signal.award_amount = Decimal("100000")
    signal.signal_date = date(2024, 1, 10)

    config = MagicMock()
    config.id = scoring_config_id
    config.config_hash = "def456"

    db = MagicMock()
    db.get.return_value = company

    def execute_side_effect(query):
        result = MagicMock()
        query_str = str(query)
        if "evidence_items" in query_str.lower() or "EvidenceItem" in str(query):
            result.scalars.return_value.all.return_value = [evidence]
            result.scalars.return_value.first.return_value = None
        elif "signals" in query_str.lower() or "Signal" in str(query):
            result.scalars.return_value.all.return_value = [signal]
            result.scalars.return_value.first.return_value = None
        elif "scoring_configs" in query_str.lower() or "ScoringConfig" in str(query):
            result.scalars.return_value.first.return_value = config
        elif "lead_candidates" in query_str.lower() or "LeadCandidate" in str(query):
            result.scalars.return_value.first.return_value = None
        else:
            result.scalars.return_value.all.return_value = []
            result.scalars.return_value.first.return_value = None
        return result

    db.execute.side_effect = execute_side_effect

    with patch("app.processing.scoring.evaluate_mandatory_gates") as mock_gates:
        mock_gates.return_value = {
            "passed": True,
            "should_score": True,
            "gate_name": None,
            "gate_reason": None,
            "route": "score",
            "suppression": None,
        }
        with patch("app.processing.scoring.flag_excluded_sector"):
            result = score_company(company_id, db)

    assert result["scored"] is True
    why_now = result["component_breakdown"]["why_now"]["points"]
    assert why_now == 4, f"Expected why_now=4 (SBA Active bonus), got {why_now}"


# ─── Pydantic record model ────────────────────────────────────────────────────


def test_record_model_valid_payload():
    """A fully valid CSV row must parse without error."""
    record = SBALoanRecord.model_validate(_valid_row())
    assert record.borr_name == "Acme Manufacturing LLC"
    assert record.borr_state == "AL"
    assert record.gross_approval == Decimal("500000")
    assert record.approval_date == date(2023, 6, 15)
    assert record.loan_status == "P I F"


def test_record_model_strips_whitespace_from_name():
    """Leading/trailing whitespace in BorrName must be stripped."""
    raw = _valid_row(borrname="  Test Corp  ")
    record = SBALoanRecord.model_validate(raw)
    assert record.borr_name == "Test Corp"


def test_record_model_normalizes_state_to_uppercase():
    """State code must be uppercased."""
    raw = _valid_row(borrstate="al")
    record = SBALoanRecord.model_validate(raw)
    assert record.borr_state == "AL"


def test_record_model_rejects_empty_borr_name():
    """Empty BorrName must raise ValidationError."""
    with pytest.raises(ValidationError):
        SBALoanRecord.model_validate(_valid_row(borrname=""))


def test_record_model_rejects_placeholder_borr_name():
    """A known placeholder BorrName must raise ValidationError (hard block)."""
    with pytest.raises(ValidationError):
        SBALoanRecord.model_validate(_valid_row(borrname="Undisclosed Recipient"))


def test_record_model_rejects_zero_amount():
    """grossapproval=0 must raise ValidationError."""
    with pytest.raises(ValidationError):
        SBALoanRecord.model_validate(_valid_row(grossapproval="0"))


def test_record_model_rejects_negative_amount():
    """GrossApproval < 0 must raise ValidationError."""
    with pytest.raises(ValidationError):
        SBALoanRecord.model_validate(_valid_row(grossapproval="-50000"))


def test_record_model_rejects_unparseable_date():
    """Unparseable ApprovalDate must raise ValidationError."""
    with pytest.raises(ValidationError):
        SBALoanRecord.model_validate(_valid_row(approvaldate="not-a-date"))


def test_record_model_parses_slash_date_format():
    """ApprovalDate in MM/DD/YYYY format must parse correctly."""
    raw = _valid_row(approvaldate="06/15/2023")
    record = SBALoanRecord.model_validate(raw)
    assert record.approval_date == date(2023, 6, 15)


def test_record_model_jobs_supported_parses_float_string():
    """jobssupported='10.0' (common in CSV) must parse to int 10."""
    raw = _valid_row(jobssupported="10.0")
    record = SBALoanRecord.model_validate(raw)
    assert record.jobs_supported == 10


def test_record_model_jobs_supported_none_when_blank():
    """Blank JobsSupported must produce None."""
    raw = _valid_row(jobssupported="")
    record = SBALoanRecord.model_validate(raw)
    assert record.jobs_supported is None


# ─── Helper function coverage ─────────────────────────────────────────────────


def test_is_included_naics_manufacturing():
    """NAICS 332312 (Manufacturing sector 33) must be included."""
    assert _is_included_naics("332312") is True


def test_is_included_naics_retail_excluded():
    """NAICS 441110 (Retail sector 44) must be excluded."""
    assert _is_included_naics("441110") is False


def test_is_included_naics_restaurant_excluded():
    """NAICS 722511 (Restaurant sector 72) must be excluded."""
    assert _is_included_naics("722511") is False


def test_is_included_naics_none_returns_false():
    """None NAICS code must return False."""
    assert _is_included_naics(None) is False


def test_is_included_naics_empty_returns_false():
    """Empty string NAICS code must return False."""
    assert _is_included_naics("") is False


def test_signal_type_pif():
    """LoanStatus 'PIF' (old format) must map to SBA_LOAN_PIF."""
    assert _signal_type_for_status("PIF") == "SBA_LOAN_PIF"


def test_signal_type_pif_with_spaces():
    """LoanStatus 'P I F' (new SBA CSV format) must map to SBA_LOAN_PIF."""
    assert _signal_type_for_status("P I F") == "SBA_LOAN_PIF"


def test_signal_type_active_blank():
    """Blank LoanStatus must map to SBA_LOAN_ACTIVE."""
    assert _signal_type_for_status("") == "SBA_LOAN_ACTIVE"


def test_signal_type_active_none():
    """None LoanStatus must map to SBA_LOAN_ACTIVE."""
    assert _signal_type_for_status(None) == "SBA_LOAN_ACTIVE"


def test_signal_type_exempt_is_active():
    """EXEMPT must map to SBA_LOAN_ACTIVE, not be skipped."""
    assert _signal_type_for_status("EXEMPT") == "SBA_LOAN_ACTIVE"


def test_signal_type_commit_is_pending():
    """COMMIT must map to SBA_LOAN_PENDING."""
    assert _signal_type_for_status("COMMIT") == "SBA_LOAN_PENDING"


# ─── Test limit env var ───────────────────────────────────────────────────────


def test_test_limit_stops_processing_early():
    """SBA_LOANS_TEST_LIMIT=2 must stop after processing 2 rows from a 5-row file."""
    rows = [_valid_row(borrname=f"Company {i}") for i in range(5)]
    source_run, _ = _run_connector(rows, env={"SBA_LOANS_TEST_LIMIT": "2"})
    assert source_run.records_fetched == 2


# ─── Stale-cache fallback when the remote download 404s ──────────────────────


def _write_stale_cache(tmp_path, age_days: int = 40):
    cache = tmp_path / "sba_loans_cache.csv"
    cache.write_text("borrname,borrstate,grossapproval,approvaldate,loanstatus\n")
    stale_mtime = time.time() - age_days * 86400
    os.utime(cache, (stale_mtime, stale_mtime))
    return cache


def test_ensure_cache_falls_back_to_stale_cache_on_404(tmp_path, monkeypatch):
    """A 404 on download must not fail the run if a stale cache is available."""
    cache = _write_stale_cache(tmp_path)
    monkeypatch.setenv("SBA_CACHE_PATH", str(cache))

    response = MagicMock(status_code=404)
    error = httpx.HTTPStatusError("404", request=MagicMock(), response=response)
    with patch("app.pipeline.connectors.sba_loans._download_csv", side_effect=error):
        result = _ensure_cache(MagicMock())

    assert result == cache
    assert cache.exists()


def test_ensure_cache_raises_when_no_cache_and_download_fails(tmp_path, monkeypatch):
    """A 404 with no existing cache at all must still raise — nothing to serve."""
    missing_cache = tmp_path / "does_not_exist.csv"
    monkeypatch.setenv("SBA_CACHE_PATH", str(missing_cache))

    response = MagicMock(status_code=404)
    error = httpx.HTTPStatusError("404", request=MagicMock(), response=response)
    with patch("app.pipeline.connectors.sba_loans._download_csv", side_effect=error):
        with pytest.raises(httpx.HTTPStatusError):
            _ensure_cache(MagicMock())


# ─── Status completes cleanly on empty CSV ───────────────────────────────────


def test_empty_csv_completes_cleanly():
    """An empty CSV (header only) must complete with status='completed' and zero counts."""
    source_run, _ = _run_connector([])
    assert source_run.status == "completed"
    assert source_run.records_fetched == 0
    assert source_run.records_valid == 0


# ─── B2C NAICS soft-flag (621/622/623/624) ───────────────────────────────────


def test_physician_office_naics_soft_flagged():
    """NAICS 621111 (Physician Offices) is collected but soft-flagged — B2C patient billing."""
    source_run, session = _run_connector([_valid_row(naicscode="621111")])
    assert source_run.records_valid == 1
    added = session.add.call_args[0][0]
    assert added.payload["sector_excluded"] is True
    assert "healthcare" in added.payload["sector_excluded_reason"].lower()


def test_dental_office_naics_soft_flagged():
    """NAICS 621210 (Dental Offices) is collected but soft-flagged — B2C patient billing."""
    source_run, session = _run_connector([_valid_row(naicscode="621210")])
    assert source_run.records_valid == 1
    added = session.add.call_args[0][0]
    assert added.payload["sector_excluded"] is True


def test_hospital_naics_soft_flagged():
    """NAICS 622110 (General Medical Hospitals) is collected but soft-flagged."""
    source_run, session = _run_connector([_valid_row(naicscode="622110")])
    assert source_run.records_valid == 1
    added = session.add.call_args[0][0]
    assert added.payload["sector_excluded"] is True


def test_nursing_facility_naics_soft_flagged():
    """NAICS 623110 (Nursing Care Facilities) is collected but soft-flagged."""
    source_run, session = _run_connector([_valid_row(naicscode="623110")])
    assert source_run.records_valid == 1
    added = session.add.call_args[0][0]
    assert added.payload["sector_excluded"] is True


def test_daycare_naics_soft_flagged():
    """NAICS 624410 (Child Day Care Services) is collected but soft-flagged."""
    source_run, session = _run_connector([_valid_row(naicscode="624410")])
    assert source_run.records_valid == 1
    added = session.add.call_args[0][0]
    assert added.payload["sector_excluded"] is True


def test_is_excluded_naics_physician():
    """_is_excluded_naics returns True for 621111 (physician office)."""
    assert _is_excluded_naics("621111") is True


def test_is_excluded_naics_hospital():
    """_is_excluded_naics returns True for 622110 (hospital)."""
    assert _is_excluded_naics("622110") is True


def test_is_excluded_naics_manufacturing_not_excluded():
    """_is_excluded_naics returns False for 332312 (manufacturing) — not healthcare."""
    assert _is_excluded_naics("332312") is False


def test_is_excluded_naics_none_returns_false():
    """_is_excluded_naics returns False for None."""
    assert _is_excluded_naics(None) is False


# ─── Noise keyword soft-flag ──────────────────────────────────────────────────


def test_dental_name_soft_flagged():
    """Company name containing 'dental' is collected but soft-flagged — B2C noise keyword."""
    source_run, session = _run_connector([_valid_row(borrname="Atlanta Dental Group LLC")])
    assert source_run.records_valid == 1
    added = session.add.call_args[0][0]
    assert added.payload["sector_excluded"] is True


def test_daycare_name_soft_flagged():
    """Company name containing 'daycare' is collected but soft-flagged — B2C noise keyword."""
    source_run, session = _run_connector([_valid_row(borrname="Sunshine Daycare LLC", naicscode="561320")])
    assert source_run.records_valid == 1
    added = session.add.call_args[0][0]
    assert added.payload["sector_excluded"] is True


def test_preschool_name_soft_flagged():
    """Company name containing 'preschool' is collected but soft-flagged — B2C noise keyword."""
    source_run, session = _run_connector([_valid_row(borrname="Bright Minds Preschool Inc", naicscode="561320")])
    assert source_run.records_valid == 1
    added = session.add.call_args[0][0]
    assert added.payload["sector_excluded"] is True


def test_chiropractic_name_soft_flagged():
    """Company name containing 'chiropractic' is collected but soft-flagged — B2C noise keyword."""
    source_run, session = _run_connector([_valid_row(borrname="Back Pain Chiropractic Center", naicscode="561320")])
    assert source_run.records_valid == 1
    added = session.add.call_args[0][0]
    assert added.payload["sector_excluded"] is True


def test_has_noise_keyword_dental():
    """_has_noise_keyword returns True for 'dental' in name."""
    assert _has_noise_keyword("Atlanta Dental Associates") is True


def test_has_noise_keyword_case_insensitive():
    """_has_noise_keyword is case-insensitive."""
    assert _has_noise_keyword("PEDIATRIC SPECIALISTS LLC") is True


def test_has_noise_keyword_clean_name():
    """_has_noise_keyword returns False for a clean B2B company name."""
    assert _has_noise_keyword("Legendary Supply Chain LLC") is False


# ─── Freshness-tiered SBA why_now scoring ────────────────────────────────────


def _make_sba_score_test_fixtures(signal_type: str, freshness: float):
    """Build company + signal mocks for a scoring test. Returns (company_id, signal, config, db)."""
    company_id = uuid.uuid4()
    scoring_config_id = uuid.uuid4()

    company = MagicMock()
    company.id = company_id
    company.naics_code = "561320"  # Staffing — not AR-heavy, isolates why_now from NAICS bonus
    company.country = "US"
    company.industry = "staffing"
    company.business_type = None

    evidence_id = uuid.uuid4()
    evidence = MagicMock()
    evidence.id = evidence_id
    evidence.source_url = "https://data.sba.gov/dataset/7-a-504-foia"
    evidence.claim_supported = signal_type
    evidence.freshness_score = Decimal(str(freshness))
    evidence.extracted_fields = {"award_amount": "100000", "action_date": "2024-01-10"}

    signal = MagicMock()
    signal.id = uuid.uuid4()
    signal.evidence_id = evidence_id
    signal.signal_type = signal_type
    signal.signal_strength = "strong" if signal_type == "SBA_LOAN_PIF" else "medium"
    signal.freshness_score = Decimal(str(freshness))
    signal.award_amount = Decimal("100000")
    signal.signal_date = date(2024, 1, 10)

    config = MagicMock()
    config.id = scoring_config_id
    config.config_hash = "test_hash"

    db = MagicMock()
    db.get.return_value = company

    def execute_side_effect(query):
        result = MagicMock()
        query_str = str(query)
        if "evidence_items" in query_str.lower() or "EvidenceItem" in str(query):
            result.scalars.return_value.all.return_value = [evidence]
            result.scalars.return_value.first.return_value = None
        elif "signals" in query_str.lower() or "Signal" in str(query):
            result.scalars.return_value.all.return_value = [signal]
            result.scalars.return_value.first.return_value = None
        elif "scoring_configs" in query_str.lower() or "ScoringConfig" in str(query):
            result.scalars.return_value.first.return_value = config
        elif "lead_candidates" in query_str.lower() or "LeadCandidate" in str(query):
            result.scalars.return_value.first.return_value = None
        else:
            result.scalars.return_value.all.return_value = []
            result.scalars.return_value.first.return_value = None
        return result

    db.execute.side_effect = execute_side_effect
    return company_id, db


def _run_scoring(signal_type: str, freshness: float) -> dict:
    from app.processing.scoring import score_company

    company_id, db = _make_sba_score_test_fixtures(signal_type, freshness)
    with patch("app.processing.scoring.evaluate_mandatory_gates") as mock_gates:
        mock_gates.return_value = {
            "passed": True,
            "should_score": True,
            "gate_name": None,
            "gate_reason": None,
            "route": "score",
            "suppression": None,
        }
        with patch("app.processing.scoring.flag_excluded_sector"):
            return score_company(company_id, db)


def test_sba_pif_fresh_why_now_is_8():
    """PIF signal with freshness >= 0.8 must contribute +8 to why_now."""
    result = _run_scoring("SBA_LOAN_PIF", 0.85)
    assert result["component_breakdown"]["why_now"]["points"] == 8


def test_sba_pif_moderate_freshness_why_now_is_6():
    """PIF signal with freshness 0.5–0.79 must contribute +6 to why_now."""
    result = _run_scoring("SBA_LOAN_PIF", 0.65)
    assert result["component_breakdown"]["why_now"]["points"] == 6


def test_sba_pif_older_freshness_why_now_is_4():
    """PIF signal with freshness 0.1–0.49 must contribute +4 to why_now."""
    result = _run_scoring("SBA_LOAN_PIF", 0.30)
    assert result["component_breakdown"]["why_now"]["points"] == 4


def test_sba_active_fresh_why_now_is_4():
    """ACTIVE signal with freshness >= 0.8 must contribute +4 to why_now."""
    result = _run_scoring("SBA_LOAN_ACTIVE", 0.85)
    assert result["component_breakdown"]["why_now"]["points"] == 4


def test_sba_active_moderate_freshness_why_now_is_3():
    """ACTIVE signal with freshness 0.5–0.79 must contribute +3 to why_now."""
    result = _run_scoring("SBA_LOAN_ACTIVE", 0.60)
    assert result["component_breakdown"]["why_now"]["points"] == 3


def test_sba_active_older_freshness_why_now_is_2():
    """ACTIVE signal with freshness 0.1–0.49 must contribute +2 to why_now."""
    result = _run_scoring("SBA_LOAN_ACTIVE", 0.25)
    assert result["component_breakdown"]["why_now"]["points"] == 2


def test_sba_pif_scores_higher_than_active_same_freshness():
    """PIF signals must always produce higher why_now than ACTIVE at same freshness."""
    for freshness in (0.85, 0.65, 0.30):
        pif = _run_scoring("SBA_LOAN_PIF", freshness)
        active = _run_scoring("SBA_LOAN_ACTIVE", freshness)
        pif_wn = pif["component_breakdown"]["why_now"]["points"]
        active_wn = active["component_breakdown"]["why_now"]["points"]
        assert pif_wn > active_wn, (
            f"freshness={freshness}: PIF why_now={pif_wn} should exceed Active why_now={active_wn}"
        )


def test_sba_loan_contributes_to_ar_fit():
    """An SBA signal must contribute the +3 lending bonus to ar_fit."""
    from app.processing.scoring import score_company

    company_id = uuid.uuid4()
    company = MagicMock()
    company.id = company_id
    company.naics_code = "519190"  # Information sector 51 — not in _AR_HEAVY_NAICS
    company.country = "US"
    company.industry = "information"
    company.business_type = None

    evidence_id = uuid.uuid4()
    evidence = MagicMock()
    evidence.id = evidence_id
    evidence.source_url = "https://data.sba.gov/dataset/7-a-504-foia"
    evidence.claim_supported = "SBA_LOAN_PIF"
    evidence.freshness_score = Decimal("0.85")
    evidence.extracted_fields = {"award_amount": "100000", "action_date": "2024-01-10"}

    signal = MagicMock()
    signal.id = uuid.uuid4()
    signal.evidence_id = evidence_id
    signal.signal_type = "SBA_LOAN_PIF"
    signal.signal_strength = "strong"
    signal.freshness_score = Decimal("0.85")
    signal.award_amount = Decimal("100000")
    signal.signal_date = date(2024, 1, 10)

    config = MagicMock()
    config.id = uuid.uuid4()
    config.config_hash = "ar_fit_test"

    db = MagicMock()
    db.get.return_value = company

    def execute_side_effect(query):
        result = MagicMock()
        query_str = str(query)
        if "evidence_items" in query_str.lower() or "EvidenceItem" in str(query):
            result.scalars.return_value.all.return_value = [evidence]
            result.scalars.return_value.first.return_value = None
        elif "signals" in query_str.lower() or "Signal" in str(query):
            result.scalars.return_value.all.return_value = [signal]
            result.scalars.return_value.first.return_value = None
        elif "scoring_configs" in query_str.lower() or "ScoringConfig" in str(query):
            result.scalars.return_value.first.return_value = config
        elif "lead_candidates" in query_str.lower() or "LeadCandidate" in str(query):
            result.scalars.return_value.first.return_value = None
        else:
            result.scalars.return_value.all.return_value = []
            result.scalars.return_value.first.return_value = None
        return result

    db.execute.side_effect = execute_side_effect

    with patch("app.processing.scoring.evaluate_mandatory_gates") as mock_gates:
        mock_gates.return_value = {
            "passed": True,
            "should_score": True,
            "gate_name": None,
            "gate_reason": None,
            "route": "score",
            "suppression": None,
        }
        with patch("app.processing.scoring.flag_excluded_sector"):
            result = score_company(company_id, db)

    # NAICS 519190 is NOT in _AR_HEAVY_NAICS (no +7), so ar_fit = 0 + 3 (lending signal) = 3
    ar_points = result["component_breakdown"]["ar_fit"]["points"]
    assert ar_points == 3, f"Expected ar_fit=3 (lending bonus only, non-AR NAICS), got {ar_points}"
