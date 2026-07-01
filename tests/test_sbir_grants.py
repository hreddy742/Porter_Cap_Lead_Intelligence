"""
Tests for the SBIR/STTR grants connector and pipeline integration.

All connector tests use mocked HTTP — no real network calls, no database.
The session is a MagicMock configured per-test for dedup behaviour.

Coverage:
  Connector — filtering (university, state, year, amount)
  Connector — Pydantic validation (SBIRGrantRecord)
  Connector — deduplication (content hash)
  Connector — phase → signal_strength mapping
  Evidence  — SBIR payload dispatch and field extraction
  Signals   — SBIR_GRANT claim handled, strength from phase
  Scoring   — SBIR_GRANT strong +6, medium +3 why_now bonus
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch, mock_open

import pytest
from pydantic import ValidationError

from app.pipeline.connectors.sbir_grants import (
    SBIRGrantRecord,
    SBIRGrantsConnector,
    is_academic,
    signal_strength_for_phase,
    _parse_response,
    _TARGET_STATES,
    _MIN_AWARD_YEAR,
    _MIN_AWARD_AMOUNT,
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


def _valid_award(**overrides) -> dict:
    """Minimal valid SBIR API award record that passes all filters."""
    base = {
        "firm": "Acme Technology Solutions LLC",
        "address1": "100 Innovation Drive",
        "city": "Birmingham",
        "state": "AL",
        "zip": "35201",
        "award_amount": 750000,
        "award_year": 2024,
        "agency": "DOD",
        "branch": "Army",
        "phase": "Phase II",
        "program": "SBIR",
        "award_title": "Advanced Materials Development",
        "abstract": "This project develops advanced composite materials for defense applications.",
        "uei": "ABCD1234567E",
        "duns": "123456789",
    }
    base.update(overrides)
    return base


def _run_connector(
    api_responses: list[list[dict]],
    *,
    session: MagicMock | None = None,
    source: MagicMock | None = None,
    source_run: MagicMock | None = None,
) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Run connector with mocked _fetch_page. Returns (connector, session, source_run)."""
    if session is None:
        session = _make_session()
    if source is None:
        source = _make_source()
    if source_run is None:
        source_run = _make_source_run()

    page_responses = list(api_responses)

    def mock_fetch_page(state, start, page_size, timeout, log):
        if page_responses:
            return page_responses.pop(0)
        return []

    with patch("app.pipeline.connectors.sbir_grants._fetch_page", side_effect=mock_fetch_page):
        connector = SBIRGrantsConnector(session, source_run, source)
        connector.run()

    return connector, session, source_run


# ─── Unit tests: is_academic() ────────────────────────────────────────────────


class TestIsAcademic:
    def test_university_filtered(self):
        assert is_academic("MIT") is False  # MIT alone is not caught
        assert is_academic("University of Alabama") is True
        assert is_academic("Alabama University") is True

    def test_college_filtered(self):
        assert is_academic("Birmingham Community College") is True
        assert is_academic("Acme College of Technology") is True

    def test_institute_filtered(self):
        assert is_academic("Georgia Tech Research Institute") is True
        assert is_academic("Institute for Advanced Studies") is True

    def test_laboratory_filtered(self):
        assert is_academic("Oak Ridge National Laboratory") is True
        assert is_academic("Argonne Laboratories Inc") is True

    def test_research_foundation_filtered(self):
        assert is_academic("Ohio Research Foundation") is True

    def test_normal_company_passes(self):
        assert is_academic("Acme Technology Solutions LLC") is False
        assert is_academic("Blue Sky Innovations Inc") is False
        assert is_academic("Defense Tech Corp") is False

    def test_empty_string(self):
        assert is_academic("") is False


# ─── Unit tests: signal_strength_for_phase() ─────────────────────────────────


class TestSignalStrengthForPhase:
    def test_phase_ii_strong(self):
        assert signal_strength_for_phase("Phase II") == "strong"
        assert signal_strength_for_phase("II") == "strong"
        assert signal_strength_for_phase("2") == "strong"
        assert signal_strength_for_phase("PHASE II") == "strong"

    def test_phase_iii_strong(self):
        assert signal_strength_for_phase("Phase III") == "strong"
        assert signal_strength_for_phase("III") == "strong"
        assert signal_strength_for_phase("3") == "strong"

    def test_phase_i_medium(self):
        assert signal_strength_for_phase("Phase I") == "medium"
        assert signal_strength_for_phase("I") == "medium"
        assert signal_strength_for_phase("1") == "medium"

    def test_none_medium(self):
        assert signal_strength_for_phase(None) == "medium"

    def test_empty_medium(self):
        assert signal_strength_for_phase("") == "medium"

    def test_unknown_medium(self):
        assert signal_strength_for_phase("Unknown") == "medium"


# ─── Unit tests: SBIRGrantRecord validation ───────────────────────────────────


class TestSBIRGrantRecord:
    def test_valid_record(self):
        r = SBIRGrantRecord.model_validate(_valid_award())
        assert r.firm == "Acme Technology Solutions LLC"
        assert r.state == "AL"
        assert r.award_amount == Decimal("750000")
        assert r.award_year == 2024

    def test_empty_firm_raises(self):
        with pytest.raises(ValidationError):
            SBIRGrantRecord.model_validate(_valid_award(firm=""))

    def test_none_firm_raises(self):
        with pytest.raises(ValidationError):
            SBIRGrantRecord.model_validate(_valid_award(firm=None))

    def test_state_uppercased(self):
        r = SBIRGrantRecord.model_validate(_valid_award(state="al"))
        assert r.state == "AL"

    def test_award_amount_string_parsed(self):
        r = SBIRGrantRecord.model_validate(_valid_award(award_amount="750,000"))
        assert r.award_amount == Decimal("750000")

    def test_award_amount_zero_valid(self):
        r = SBIRGrantRecord.model_validate(_valid_award(award_amount=0))
        assert r.award_amount == Decimal("0")

    def test_award_year_string_parsed(self):
        r = SBIRGrantRecord.model_validate(_valid_award(award_year="2023"))
        assert r.award_year == 2023

    def test_optional_fields_none(self):
        minimal = {"firm": "Acme LLC", "state": "AL", "award_amount": 100000, "award_year": 2023}
        r = SBIRGrantRecord.model_validate(minimal)
        assert r.city is None
        assert r.uei is None
        assert r.phase is None

    def test_extra_fields_ignored(self):
        data = _valid_award()
        data["unknown_field"] = "some value"
        r = SBIRGrantRecord.model_validate(data)
        assert r.firm == "Acme Technology Solutions LLC"


# ─── Unit tests: _parse_response() ───────────────────────────────────────────


class TestParseResponse:
    def test_direct_list(self):
        data = [{"firm": "A"}, {"firm": "B"}]
        assert _parse_response(data) == data

    def test_solr_response_docs(self):
        data = {"response": {"docs": [{"firm": "A"}], "numFound": 1}}
        assert _parse_response(data) == [{"firm": "A"}]

    def test_docs_top_level(self):
        data = {"docs": [{"firm": "A"}]}
        assert _parse_response(data) == [{"firm": "A"}]

    def test_empty_list(self):
        assert _parse_response([]) == []

    def test_empty_dict(self):
        assert _parse_response({}) == []

    def test_none_like(self):
        assert _parse_response("not a list") == []


# ─── Connector filter tests ───────────────────────────────────────────────────


class TestConnectorFilters:
    def _run_single(self, award_overrides: dict, **connector_kwargs):
        """Run connector with a single award. Returns source_run."""
        award = _valid_award(**award_overrides)
        _, _, source_run = _run_connector([[award], []], **connector_kwargs)
        return source_run

    def test_university_skipped(self):
        run = self._run_single({"firm": "University of Alabama Research LLC"})
        assert run.records_valid == 0
        assert run.records_skipped == 1

    def test_college_skipped(self):
        run = self._run_single({"firm": "Auburn Community College Tech Transfer"})
        assert run.records_valid == 0
        assert run.records_skipped == 1

    def test_empty_firm_quarantined(self):
        _, _, run = _run_connector([[{"firm": "", "state": "AL", "award_amount": 100000, "award_year": 2024}], []])
        assert run.quarantine_count == 1
        assert run.records_valid == 0

    def test_award_year_before_2022_skipped(self):
        run = self._run_single({"award_year": 2021})
        assert run.records_valid == 0
        assert run.records_skipped == 1

    def test_award_year_2022_passes(self):
        run = self._run_single({"award_year": 2022})
        assert run.records_valid == 1

    def test_state_not_in_target_skipped(self):
        run = self._run_single({"state": "CA"})
        assert run.records_valid == 0
        assert run.records_skipped == 1

    def test_state_in_target_passes(self):
        for state in ["AL", "GA", "TN", "FL", "MS", "TX", "VA"]:
            run = self._run_single({"state": state})
            assert run.records_valid == 1, f"Expected valid for state={state}"

    def test_award_below_minimum_skipped(self):
        run = self._run_single({"award_amount": 49999})
        assert run.records_valid == 0
        assert run.records_skipped == 1

    def test_award_at_minimum_passes(self):
        run = self._run_single({"award_amount": 50000})
        assert run.records_valid == 1

    def test_phase_ii_stored_as_strong(self):
        session = _make_session()
        source_run = _make_source_run()
        award = _valid_award(phase="Phase II")
        with patch("app.pipeline.connectors.sbir_grants._fetch_page", side_effect=[[award], []]):
            connector = SBIRGrantsConnector(session, source_run, _make_source())
            connector.run()
        added_events = [c for c in session.add.call_args_list]
        assert source_run.records_valid == 1
        event_args = session.add.call_args_list[0][0][0]
        assert event_args.payload["sbir_signal_strength"] == "strong"

    def test_phase_i_stored_as_medium(self):
        session = _make_session()
        source_run = _make_source_run()
        award = _valid_award(phase="Phase I")
        with patch("app.pipeline.connectors.sbir_grants._fetch_page", side_effect=[[award], []]):
            connector = SBIRGrantsConnector(session, source_run, _make_source())
            connector.run()
        event_args = session.add.call_args_list[0][0][0]
        assert event_args.payload["sbir_signal_strength"] == "medium"

    def test_deduplication_same_company_twice(self):
        award = _valid_award()
        session_new = _make_session(record_exists=False)
        source_run = _make_source_run()
        # First pass: not in DB yet
        with patch("app.pipeline.connectors.sbir_grants._fetch_page", side_effect=[[award], []]):
            connector = SBIRGrantsConnector(session_new, source_run, _make_source())
            connector.run()
        assert source_run.records_valid == 1

        # Second pass: same record now exists in DB
        source_run2 = _make_source_run()
        session_exists = _make_session(record_exists=True)
        with patch("app.pipeline.connectors.sbir_grants._fetch_page", side_effect=[[award], []]):
            connector2 = SBIRGrantsConnector(session_exists, source_run2, _make_source())
            connector2.run()
        assert source_run2.records_valid == 0
        assert source_run2.records_skipped == 1

    def test_evidence_fields_stored_correctly(self):
        session = _make_session()
        source_run = _make_source_run()
        award = _valid_award(
            firm="Defense Tech Corp",
            state="GA",
            award_amount=500000,
            award_year=2023,
            agency="NASA",
            branch="Science",
            phase="Phase I",
            uei="XYZ9876543AB",
        )
        with patch("app.pipeline.connectors.sbir_grants._fetch_page", side_effect=[[award], []]):
            connector = SBIRGrantsConnector(session, source_run, _make_source())
            connector.run()
        assert source_run.records_valid == 1
        event = session.add.call_args_list[0][0][0]
        payload = event.payload
        assert payload["firm"] == "Defense Tech Corp"
        assert payload["state"] == "GA"
        assert payload["award_amount"] == "500000"
        assert payload["award_year"] == 2023
        assert payload["agency"] == "NASA"
        assert payload["uei"] == "XYZ9876543AB"
        assert payload["sbir_signal_type"] == "SBIR_GRANT"
        assert payload["sbir_signal_strength"] == "medium"
        assert event.company_name_raw == "Defense Tech Corp"


# ─── Evidence extraction tests ───────────────────────────────────────────────


class TestSBIREvidenceExtraction:
    def _make_raw_event(self, payload: dict) -> MagicMock:
        evt = MagicMock()
        evt.id = uuid.uuid4()
        evt.source_id = uuid.uuid4()
        evt.payload = payload
        evt.source_url = "https://www.sbir.gov/awards"
        return evt

    def test_sbir_payload_dispatched(self):
        from app.processing.evidence import extract_evidence

        payload = {
            "sbir_signal_type": "SBIR_GRANT",
            "firm": "Acme Tech LLC",
            "state": "AL",
            "award_amount": "750000",
            "award_year": 2024,
            "phase": "Phase II",
            "sbir_signal_strength": "strong",
            "agency": "DOD",
        }
        raw_event = self._make_raw_event(payload)

        db = MagicMock()
        db.get.return_value = raw_event
        db.flush = MagicMock()

        items = extract_evidence(raw_event.id, db)
        assert len(items) == 1
        item = items[0]
        assert item.claim_supported == "SBIR_GRANT"
        assert item.extracted_fields["company_name"] == "Acme Tech LLC"
        assert item.extracted_fields["award_year"] == 2024
        assert item.extracted_fields["sbir_signal_strength"] == "strong"
        # action_date should be mid-year of award_year
        assert item.extracted_fields["action_date"] == "2024-06-15"
        assert float(item.freshness_score) > 0.0

    def test_missing_firm_quarantined(self):
        from app.processing.evidence import extract_evidence

        payload = {
            "sbir_signal_type": "SBIR_GRANT",
            "firm": "",
            "state": "AL",
            "award_amount": "750000",
            "award_year": 2024,
        }
        raw_event = self._make_raw_event(payload)

        db = MagicMock()
        db.get.return_value = raw_event
        items = extract_evidence(raw_event.id, db)
        assert items == []


# ─── Signal detection tests ───────────────────────────────────────────────────


class TestSBIRSignalDetection:
    def _make_evidence(self, *, strength: str = "strong") -> MagicMock:
        ev = MagicMock()
        ev.id = uuid.uuid4()
        ev.company_id = uuid.uuid4()
        ev.source_id = uuid.uuid4()
        ev.claim_supported = "SBIR_GRANT"
        ev.freshness_score = Decimal("0.8")
        ev.extracted_fields = {
            "company_name": "Acme Tech LLC",
            "award_amount": "750000",
            "action_date": "2024-06-15",
            "sbir_signal_strength": strength,
        }
        return ev

    def test_sbir_grant_handled(self):
        from app.processing.signals import detect_signals_for_evidence

        evidence = self._make_evidence(strength="strong")
        db = MagicMock()
        db.get.return_value = evidence
        db.execute.return_value.scalars.return_value.all.return_value = []
        db.flush = MagicMock()

        signals = detect_signals_for_evidence(evidence.id, db)
        assert len(signals) == 1
        sig = signals[0]
        assert sig.signal_type == "SBIR_GRANT"
        assert sig.signal_strength == "strong"

    def test_phase_i_medium_strength(self):
        from app.processing.signals import detect_signals_for_evidence

        evidence = self._make_evidence(strength="medium")
        db = MagicMock()
        db.get.return_value = evidence
        db.execute.return_value.scalars.return_value.all.return_value = []
        db.flush = MagicMock()

        signals = detect_signals_for_evidence(evidence.id, db)
        assert signals[0].signal_strength == "medium"


# ─── Scoring bonus tests ──────────────────────────────────────────────────────


def _make_sbir_scoring_db(company, evidence_item, signal, config) -> MagicMock:
    """Build a MagicMock DB that dispatches queries by model name, like test_sba_loans.py."""
    db = MagicMock()
    db.get.return_value = company

    def execute_side_effect(query):
        result = MagicMock()
        query_str = str(query)
        if "evidence_items" in query_str.lower() or "EvidenceItem" in str(query):
            result.scalars.return_value.all.return_value = [evidence_item]
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
    return db


def test_sbir_grant_strong_adds_6_why_now():
    """A company with an SBIR_GRANT strong signal must receive +6 to why_now."""
    from app.processing.scoring import score_company

    company_id = uuid.uuid4()
    evidence_id = uuid.uuid4()

    company = MagicMock()
    company.id = company_id
    company.naics_code = "541715"
    company.country = "US"
    company.industry = "technology"
    company.business_type = None

    evidence = MagicMock()
    evidence.id = evidence_id
    evidence.source_url = "https://www.sbir.gov/awards"

    signal = MagicMock()
    signal.id = uuid.uuid4()
    signal.evidence_id = evidence_id
    signal.signal_type = "SBIR_GRANT"
    signal.signal_strength = "strong"
    signal.freshness_score = Decimal("0.85")
    signal.award_amount = Decimal("750000")
    signal.signal_date = date(2024, 6, 15)

    config = MagicMock()
    config.id = uuid.uuid4()
    config.config_hash = "abc123"

    db = _make_sbir_scoring_db(company, evidence, signal, config)

    with patch("app.processing.scoring.evaluate_mandatory_gates") as mock_gates:
        mock_gates.return_value = {
            "passed": True, "should_score": True,
            "gate_name": None, "gate_reason": None,
            "route": "score", "suppression": None,
        }
        with patch("app.processing.scoring.flag_excluded_sector"):
            result = score_company(company_id, db)

    assert result["scored"] is True
    why_now = result["component_breakdown"]["why_now"]["points"]
    assert why_now == 6, f"Expected why_now=6 (SBIR strong bonus), got {why_now}"


def test_sbir_grant_medium_adds_3_why_now():
    """A company with an SBIR_GRANT medium signal must receive +3 to why_now."""
    from app.processing.scoring import score_company

    company_id = uuid.uuid4()
    evidence_id = uuid.uuid4()

    company = MagicMock()
    company.id = company_id
    company.naics_code = "541715"
    company.country = "US"
    company.industry = "technology"
    company.business_type = None

    evidence = MagicMock()
    evidence.id = evidence_id
    evidence.source_url = "https://www.sbir.gov/awards"

    signal = MagicMock()
    signal.id = uuid.uuid4()
    signal.evidence_id = evidence_id
    signal.signal_type = "SBIR_GRANT"
    signal.signal_strength = "medium"
    signal.freshness_score = Decimal("0.85")
    signal.award_amount = Decimal("150000")
    signal.signal_date = date(2024, 6, 15)

    config = MagicMock()
    config.id = uuid.uuid4()
    config.config_hash = "abc123"

    db = _make_sbir_scoring_db(company, evidence, signal, config)

    with patch("app.processing.scoring.evaluate_mandatory_gates") as mock_gates:
        mock_gates.return_value = {
            "passed": True, "should_score": True,
            "gate_name": None, "gate_reason": None,
            "route": "score", "suppression": None,
        }
        with patch("app.processing.scoring.flag_excluded_sector"):
            result = score_company(company_id, db)

    assert result["scored"] is True
    why_now = result["component_breakdown"]["why_now"]["points"]
    assert why_now == 3, f"Expected why_now=3 (SBIR medium bonus), got {why_now}"


def test_sbir_why_now_capped_at_30():
    """SBIR bonus cannot push why_now above the 30-point ceiling."""
    from app.processing.scoring import score_company

    company_id = uuid.uuid4()
    evidence_id = uuid.uuid4()

    company = MagicMock()
    company.id = company_id
    company.naics_code = "541715"
    company.country = "US"
    company.industry = "technology"
    company.business_type = None

    evidence = MagicMock()
    evidence.id = evidence_id
    evidence.source_url = "https://www.sbir.gov/awards"

    # Contract signal already filling why_now to 30 (freshness=1.0)
    contract_signal = MagicMock()
    contract_signal.id = uuid.uuid4()
    contract_signal.evidence_id = evidence_id
    contract_signal.signal_type = "CONTRACT_AWARD"
    contract_signal.signal_strength = "strong"
    contract_signal.freshness_score = Decimal("1.0")
    contract_signal.award_amount = Decimal("2000000")
    contract_signal.signal_date = date(2024, 6, 15)

    sbir_signal = MagicMock()
    sbir_signal.id = uuid.uuid4()
    sbir_signal.evidence_id = evidence_id
    sbir_signal.signal_type = "SBIR_GRANT"
    sbir_signal.signal_strength = "strong"
    sbir_signal.freshness_score = Decimal("0.85")
    sbir_signal.award_amount = Decimal("750000")
    sbir_signal.signal_date = date(2024, 6, 15)

    config = MagicMock()
    config.id = uuid.uuid4()
    config.config_hash = "abc123"

    db = MagicMock()
    db.get.return_value = company

    def execute_side_effect(query):
        result = MagicMock()
        query_str = str(query)
        if "evidence_items" in query_str.lower() or "EvidenceItem" in str(query):
            result.scalars.return_value.all.return_value = [evidence]
            result.scalars.return_value.first.return_value = None
        elif "signals" in query_str.lower() or "Signal" in str(query):
            result.scalars.return_value.all.return_value = [contract_signal, sbir_signal]
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
            "passed": True, "should_score": True,
            "gate_name": None, "gate_reason": None,
            "route": "score", "suppression": None,
        }
        with patch("app.processing.scoring.flag_excluded_sector"):
            result = score_company(company_id, db)

    assert result["scored"] is True
    why_now = result["component_breakdown"]["why_now"]["points"]
    assert why_now <= 30, f"why_now exceeded 30: got {why_now}"
