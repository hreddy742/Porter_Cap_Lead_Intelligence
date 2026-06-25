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

import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from app.pipeline.connectors.sba_loans import (
    SBALoanRecord,
    SBALoansConnector,
    _is_included_naics,
    _signal_type_for_status,
    _SBA_INCLUDED_NAICS_PREFIXES,
    _TARGET_STATES,
    _MIN_LOAN_AMOUNT,
    _MAX_LOAN_AMOUNT,
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
    """Minimal valid SBA CSV row that passes all filters."""
    base = {
        "BorrName": "Acme Manufacturing LLC",
        "BorrStreet": "123 Main St",
        "BorrCity": "Birmingham",
        "BorrState": "AL",
        "BorrZip": "35201",
        "NaicsCode": "332312",
        "NaicsDescription": "Fabricated Structural Metal Manufacturing",
        "GrossApproval": "500000",
        "ApprovalDate": "2023-06-15",
        "LoanStatus": "PIF",
        "JobsSupported": "25",
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
        csv_content = "BorrName,BorrState,GrossApproval,ApprovalDate,LoanStatus\n"

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


# ─── NAICS filtering ─────────────────────────────────────────────────────────


def test_restaurant_naics_excluded():
    """NAICS 722511 (Restaurants) must be excluded — pure B2C."""
    source_run, _ = _run_connector([_valid_row(NaicsCode="722511", NaicsDescription="Restaurants")])
    assert source_run.records_skipped == 1
    assert source_run.records_valid == 0


def test_hotel_naics_excluded():
    """NAICS 721110 (Hotels) must be excluded — pure B2C."""
    source_run, _ = _run_connector([_valid_row(NaicsCode="721110", NaicsDescription="Hotels")])
    assert source_run.records_skipped == 1
    assert source_run.records_valid == 0


def test_retail_naics_excluded():
    """NAICS 441110 (New Car Dealers) must be excluded — Retail Trade (44)."""
    source_run, _ = _run_connector([_valid_row(NaicsCode="441110")])
    assert source_run.records_skipped == 1
    assert source_run.records_valid == 0


def test_manufacturing_naics_included():
    """NAICS 332312 (Manufacturing) must be included — B2B sector 33."""
    source_run, _ = _run_connector([_valid_row(NaicsCode="332312")])
    assert source_run.records_valid == 1
    assert source_run.records_skipped == 0


def test_staffing_naics_included():
    """NAICS 561320 (Staffing) must be included — B2B sector 56."""
    source_run, _ = _run_connector([_valid_row(NaicsCode="561320")])
    assert source_run.records_valid == 1


def test_wholesale_naics_included():
    """NAICS 423990 (Wholesale) must be included — B2B sector 42."""
    source_run, _ = _run_connector([_valid_row(NaicsCode="423990")])
    assert source_run.records_valid == 1


def test_healthcare_naics_included():
    """NAICS 621111 (Healthcare) must be included — B2B medical sector 62."""
    source_run, _ = _run_connector([_valid_row(NaicsCode="621111")])
    assert source_run.records_valid == 1


def test_construction_naics_included():
    """NAICS 238210 (Electrical Contractors) must be included — Construction sector 23."""
    source_run, _ = _run_connector([_valid_row(NaicsCode="238210")])
    assert source_run.records_valid == 1


# ─── State filtering ─────────────────────────────────────────────────────────


def test_state_ny_excluded():
    """State NY must be excluded — not in Porter's geographic ICP."""
    source_run, _ = _run_connector([_valid_row(BorrState="NY")])
    assert source_run.records_skipped == 1
    assert source_run.records_valid == 0


def test_state_al_included():
    """State AL must be included — in Porter's geographic ICP."""
    source_run, _ = _run_connector([_valid_row(BorrState="AL")])
    assert source_run.records_valid == 1


def test_all_target_states_included():
    """All seven target states must pass the state filter."""
    for state in ("AL", "GA", "TN", "FL", "MS", "TX", "VA"):
        source_run, _ = _run_connector([_valid_row(BorrState=state)])
        assert source_run.records_valid == 1, f"State {state} should be included"


# ─── Loan amount filtering ────────────────────────────────────────────────────


def test_amount_below_minimum_excluded():
    """Loan amount $30,000 must be excluded — below $50K minimum."""
    source_run, _ = _run_connector([_valid_row(GrossApproval="30000")])
    assert source_run.records_skipped == 1
    assert source_run.records_valid == 0


def test_amount_above_maximum_excluded():
    """Loan amount $6,000,000 must be excluded — above $5M maximum."""
    source_run, _ = _run_connector([_valid_row(GrossApproval="6000000")])
    assert source_run.records_skipped == 1
    assert source_run.records_valid == 0


def test_amount_500k_included():
    """Loan amount $500,000 must be included — within $50K–$5M range."""
    source_run, _ = _run_connector([_valid_row(GrossApproval="500000")])
    assert source_run.records_valid == 1


def test_amount_50k_boundary_included():
    """Loan amount exactly $50,000 must be included — at minimum boundary."""
    source_run, _ = _run_connector([_valid_row(GrossApproval="50000")])
    assert source_run.records_valid == 1


def test_amount_5m_boundary_included():
    """Loan amount exactly $5,000,000 must be included — at maximum boundary."""
    source_run, _ = _run_connector([_valid_row(GrossApproval="5000000")])
    assert source_run.records_valid == 1


# ─── LoanStatus filtering ─────────────────────────────────────────────────────


def test_chgoff_status_excluded():
    """LoanStatus CHGOFF (charged off/defaulted) must be excluded."""
    source_run, _ = _run_connector([_valid_row(LoanStatus="CHGOFF")])
    assert source_run.records_skipped == 1
    assert source_run.records_valid == 0


def test_cancld_status_excluded():
    """LoanStatus CANCLD (cancelled) must be excluded."""
    source_run, _ = _run_connector([_valid_row(LoanStatus="CANCLD")])
    assert source_run.records_skipped == 1
    assert source_run.records_valid == 0


def test_pif_status_produces_sba_loan_pif_signal():
    """LoanStatus PIF must produce signal_type SBA_LOAN_PIF in the stored payload."""
    source_run, session = _run_connector([_valid_row(LoanStatus="PIF")])
    assert source_run.records_valid == 1
    added = session.add.call_args[0][0]
    assert added.payload["sba_signal_type"] == "SBA_LOAN_PIF"


def test_blank_loan_status_produces_sba_loan_active_signal():
    """Blank LoanStatus (active loan) must produce signal_type SBA_LOAN_ACTIVE."""
    source_run, session = _run_connector([_valid_row(LoanStatus="")])
    assert source_run.records_valid == 1
    added = session.add.call_args[0][0]
    assert added.payload["sba_signal_type"] == "SBA_LOAN_ACTIVE"


# ─── Approval date filtering ─────────────────────────────────────────────────


def test_approval_date_2019_excluded():
    """ApprovalDate in 2019 must be excluded — before 2022-01-01 cutoff."""
    source_run, _ = _run_connector([_valid_row(ApprovalDate="2019-06-15")])
    assert source_run.records_skipped == 1
    assert source_run.records_valid == 0


def test_approval_date_2023_included():
    """ApprovalDate in 2023 must be included — after 2022-01-01 cutoff."""
    source_run, _ = _run_connector([_valid_row(ApprovalDate="2023-06-15")])
    assert source_run.records_valid == 1


def test_approval_date_2022_01_01_boundary_included():
    """ApprovalDate exactly 2022-01-01 must be included — at cutoff boundary."""
    source_run, _ = _run_connector([_valid_row(ApprovalDate="2022-01-01")])
    assert source_run.records_valid == 1


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
    evidence.freshness_score = Decimal("0.30")
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
    signal.freshness_score = Decimal("0.30")
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
    evidence.freshness_score = Decimal("0.50")
    evidence.extracted_fields = {"award_amount": "100000", "action_date": "2024-01-10"}

    signal = MagicMock()
    signal.id = uuid.uuid4()
    signal.evidence_id = evidence_id
    signal.signal_type = "SBA_LOAN_ACTIVE"
    signal.signal_strength = "medium"
    signal.freshness_score = Decimal("0.50")
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
    assert record.loan_status == "PIF"


def test_record_model_strips_whitespace_from_name():
    """Leading/trailing whitespace in BorrName must be stripped."""
    raw = _valid_row(BorrName="  Test Corp  ")
    record = SBALoanRecord.model_validate(raw)
    assert record.borr_name == "Test Corp"


def test_record_model_normalizes_state_to_uppercase():
    """State code must be uppercased."""
    raw = _valid_row(BorrState="al")
    record = SBALoanRecord.model_validate(raw)
    assert record.borr_state == "AL"


def test_record_model_rejects_empty_borr_name():
    """Empty BorrName must raise ValidationError."""
    with pytest.raises(ValidationError):
        SBALoanRecord.model_validate(_valid_row(BorrName=""))


def test_record_model_rejects_zero_amount():
    """GrossApproval=0 must raise ValidationError."""
    with pytest.raises(ValidationError):
        SBALoanRecord.model_validate(_valid_row(GrossApproval="0"))


def test_record_model_rejects_negative_amount():
    """GrossApproval < 0 must raise ValidationError."""
    with pytest.raises(ValidationError):
        SBALoanRecord.model_validate(_valid_row(GrossApproval="-50000"))


def test_record_model_rejects_unparseable_date():
    """Unparseable ApprovalDate must raise ValidationError."""
    with pytest.raises(ValidationError):
        SBALoanRecord.model_validate(_valid_row(ApprovalDate="not-a-date"))


def test_record_model_parses_slash_date_format():
    """ApprovalDate in MM/DD/YYYY format must parse correctly."""
    raw = _valid_row(ApprovalDate="06/15/2023")
    record = SBALoanRecord.model_validate(raw)
    assert record.approval_date == date(2023, 6, 15)


def test_record_model_jobs_supported_parses_float_string():
    """JobsSupported='10.0' (common in CSV) must parse to int 10."""
    raw = _valid_row(JobsSupported="10.0")
    record = SBALoanRecord.model_validate(raw)
    assert record.jobs_supported == 10


def test_record_model_jobs_supported_none_when_blank():
    """Blank JobsSupported must produce None."""
    raw = _valid_row(JobsSupported="")
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
    """LoanStatus 'PIF' must map to SBA_LOAN_PIF."""
    assert _signal_type_for_status("PIF") == "SBA_LOAN_PIF"


def test_signal_type_active_blank():
    """Blank LoanStatus must map to SBA_LOAN_ACTIVE."""
    assert _signal_type_for_status("") == "SBA_LOAN_ACTIVE"


def test_signal_type_active_none():
    """None LoanStatus must map to SBA_LOAN_ACTIVE."""
    assert _signal_type_for_status(None) == "SBA_LOAN_ACTIVE"


# ─── Test limit env var ───────────────────────────────────────────────────────


def test_test_limit_stops_processing_early():
    """SBA_LOANS_TEST_LIMIT=2 must stop after processing 2 rows from a 5-row file."""
    rows = [_valid_row(BorrName=f"Company {i}") for i in range(5)]
    source_run, _ = _run_connector(rows, env={"SBA_LOANS_TEST_LIMIT": "2"})
    assert source_run.records_fetched == 2


# ─── Status completes cleanly on empty CSV ───────────────────────────────────


def test_empty_csv_completes_cleanly():
    """An empty CSV (header only) must complete with status='completed' and zero counts."""
    source_run, _ = _run_connector([])
    assert source_run.status == "completed"
    assert source_run.records_fetched == 0
    assert source_run.records_valid == 0
