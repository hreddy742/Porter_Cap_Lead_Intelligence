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

import csv
import io
import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch, mock_open

import pytest
from pydantic import ValidationError

from app.pipeline.connectors.sbir_grants import (
    ConnectorError,
    SBIRGrantRecord,
    SBIRGrantsConnector,
    is_academic,
    signal_strength_for_phase,
    _is_government_contact,
    _is_exact_academic_name,
    _is_active_sbir,
    _normalize_bulk_row,
    _parse_response,
    _ALL_STATES,
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


def _valid_bulk_row(**overrides) -> dict:
    """Minimal valid SBIR bulk CSV row (Title Case headers) that passes all filters."""
    base = {
        "Company": "Acme Technology Solutions LLC",
        "Award Title": "Advanced Materials Development",
        "Agency": "Department of Defense",
        "Branch": "Army",
        "Phase": "Phase II",
        "Program": "SBIR",
        "Award Year": "2024",
        "Award Amount": "750000.0000",
        "UEI": "ABCD1234567E",
        "Duns": "123456789",
        "Number Employees": "12",
        "Company Website": "https://acmetech.com",
        "Address1": "100 Innovation Drive",
        "Address2": "",
        "City": "Birmingham",
        "State": "Alabama",
        "Zip": "35201",
        "Contact Name": "Jane Doe",
        "Contact Title": "CEO",
        "Contact Phone": "2055551234",
        "Contact Email": "jane@acmetech.com",
    }
    base.update(overrides)
    return base


def _csv_content(rows: list[dict]) -> str:
    if not rows:
        return "Company,State,Award Year,Award Amount\n"
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def _run_dual_mode(
    *,
    bulk_rows: list[dict] | None = None,
    api_responses: list[list[dict]] | None = None,
    session: MagicMock | None = None,
    source: MagicMock | None = None,
    source_run: MagicMock | None = None,
    env: dict[str, str] | None = None,
) -> tuple[SBIRGrantsConnector, MagicMock, MagicMock]:
    """Run the connector with mocked bulk cache and/or mocked API. Returns (connector, session, source_run)."""
    if session is None:
        session = _make_session()
    if source is None:
        source = _make_source()
    if source_run is None:
        source_run = _make_source_run()

    mock_path = MagicMock()
    mock_path.open.return_value.__enter__.return_value = io.StringIO(_csv_content(bulk_rows or []))

    page_responses = list(api_responses or [])

    def mock_fetch_page(state, start, page_size, timeout, log):
        if page_responses:
            return page_responses.pop(0)
        return []

    env_patch = {"SBIR_TEST_LIMIT": "0"}
    if bulk_rows is not None:
        env_patch["SBIR_BULK_ENABLED"] = "true"
    if api_responses is not None:
        env_patch["SBIR_API_ENABLED"] = "true"
    else:
        env_patch.setdefault("SBIR_API_ENABLED", "false")
    if env:
        env_patch.update(env)

    with patch("app.pipeline.connectors.sbir_grants._ensure_bulk_cache", return_value=mock_path):
        with patch("app.pipeline.connectors.sbir_grants._fetch_page", side_effect=mock_fetch_page):
            with patch.dict("os.environ", env_patch):
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

    def test_bare_institute_no_longer_broad_match(self):
        """'institute' alone was removed — too broad, collided with legitimate
        B2B names. Only the precise 'national institute of' phrase matches now."""
        assert is_academic("Georgia Tech Research Institute") is False
        assert is_academic("Institute for Advanced Studies") is False

    def test_national_institute_of_filtered(self):
        assert is_academic("National Institute of Standards and Technology") is True

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

    def test_university_soft_flagged_not_skipped(self):
        """Universities are collected and soft-flagged, not hard-blocked, unless
        the name is an exact match against the tiny hard-block list."""
        award = _valid_award(firm="University of Alabama Research LLC")
        session = _make_session()
        source_run = _make_source_run()
        with patch("app.pipeline.connectors.sbir_grants._fetch_page", side_effect=[[award], []]):
            connector = SBIRGrantsConnector(session, source_run, _make_source())
            connector.run()
        assert source_run.records_valid == 1
        assert source_run.records_skipped == 0
        event = session.add.call_args_list[0][0][0]
        assert event.payload["sector_excluded"] is True
        assert event.payload["sector_excluded_reason"] == "Academic institution"

    def test_college_soft_flagged_not_skipped(self):
        award = _valid_award(firm="Auburn Community College Tech Transfer")
        session = _make_session()
        source_run = _make_source_run()
        with patch("app.pipeline.connectors.sbir_grants._fetch_page", side_effect=[[award], []]):
            connector = SBIRGrantsConnector(session, source_run, _make_source())
            connector.run()
        assert source_run.records_valid == 1
        event = session.add.call_args_list[0][0][0]
        assert event.payload["sector_excluded"] is True

    def test_exact_academic_name_hard_blocked(self):
        """A firm name that is EXACTLY a known university is still hard-blocked."""
        run = self._run_single({"firm": "MIT"})
        assert run.records_valid == 0
        assert run.records_skipped == 1

    def test_non_academic_not_flagged(self):
        award = _valid_award(firm="Acme Technology Solutions LLC")
        session = _make_session()
        source_run = _make_source_run()
        with patch("app.pipeline.connectors.sbir_grants._fetch_page", side_effect=[[award], []]):
            connector = SBIRGrantsConnector(session, source_run, _make_source())
            connector.run()
        event = session.add.call_args_list[0][0][0]
        assert event.payload["sector_excluded"] is False
        assert event.payload["sector_excluded_reason"] is None

    def test_empty_firm_quarantined(self):
        _, _, run = _run_connector([[{"firm": "", "state": "AL", "award_amount": 100000, "award_year": 2024}], []])
        assert run.quarantine_count == 1
        assert run.records_valid == 0

    def test_award_year_before_2019_skipped(self):
        run = self._run_single({"award_year": 2015})
        assert run.records_valid == 0
        assert run.records_skipped == 1

    def test_award_year_2021_now_included(self):
        """2021 was excluded under the old 2022+ cutoff; the new 7-year window
        (2019+) includes it."""
        run = self._run_single({"award_year": 2021})
        assert run.records_valid == 1

    def test_award_year_2019_boundary_passes(self):
        run = self._run_single({"award_year": 2019})
        assert run.records_valid == 1

    def test_award_year_2022_passes(self):
        run = self._run_single({"award_year": 2022})
        assert run.records_valid == 1

    def test_future_contract_end_date_included_regardless_of_year(self):
        """An old award year with a future contract_end_date is still active."""
        run = self._run_single({"award_year": 2015, "contract_end_date": "2099-01-01"})
        assert run.records_valid == 1

    def test_state_outside_old_icp_now_included(self):
        """CA was excluded under the old 7-state ICP; the state filter is removed."""
        run = self._run_single({"state": "CA"})
        assert run.records_valid == 1
        assert run.records_skipped == 0

    def test_all_states_included(self):
        for state in ["AL", "GA", "TN", "FL", "MS", "TX", "VA", "CA", "NY", "WA"]:
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


# ─── State normalization (bulk uses full names, API uses 2-letter codes) ─────


class TestStateNormalization:
    def test_full_state_name_converted(self):
        r = SBIRGrantRecord.model_validate(_valid_award(state="Alabama"))
        assert r.state == "AL"

    def test_full_state_name_case_insensitive(self):
        r = SBIRGrantRecord.model_validate(_valid_award(state="georgia"))
        assert r.state == "GA"

    def test_abbreviation_still_passes_through(self):
        r = SBIRGrantRecord.model_validate(_valid_award(state="ga"))
        assert r.state == "GA"

    def test_unrecognized_state_raises(self):
        with pytest.raises(ValidationError):
            SBIRGrantRecord.model_validate(_valid_award(state="Nowhereland"))


# ─── number_employees parsing ─────────────────────────────────────────────────


class TestNumberEmployees:
    def test_parses_integer_string(self):
        r = SBIRGrantRecord.model_validate(_valid_award(number_employees="12"))
        assert r.number_employees == 12

    def test_none_when_blank(self):
        r = SBIRGrantRecord.model_validate(_valid_award(number_employees=""))
        assert r.number_employees is None

    def test_none_when_missing(self):
        r = SBIRGrantRecord.model_validate(_valid_award())
        assert r.number_employees is None


# ─── award_amount normalization (cross-mode dedup depends on this) ──────────


class TestAwardAmountNormalization:
    def test_decimal_fraction_rounds_to_whole_dollar(self):
        r = SBIRGrantRecord.model_validate(_valid_award(award_amount="664827.5000"))
        assert r.award_amount == Decimal("664828")

    def test_bulk_style_trailing_zeros_normalized(self):
        r = SBIRGrantRecord.model_validate(_valid_award(award_amount="750000.0000"))
        assert r.award_amount == Decimal("750000")


# ─── Expanded institution filter (hospitals, associations, foundations) ─────


class TestExpandedInstitutionFilter:
    def test_hospital_filtered(self):
        assert is_academic("Regional Medical Hospital Inc") is True

    def test_association_filtered(self):
        assert is_academic("Association of American Engineers") is True

    def test_bare_foundation_for_no_longer_broad_match(self):
        """'foundation for' alone was removed — too broad."""
        assert is_academic("Foundation for Advanced Robotics") is False

    def test_national_foundation_for_filtered(self):
        assert is_academic("National Foundation for American Policy") is True

    def test_bare_center_for_no_longer_broad_match(self):
        """'center for' alone was removed — collided with legitimate B2B names
        like 'Center for Applied Engineering LLC'."""
        assert is_academic("Center for Applied Physics LLC") is False

    def test_national_center_for_filtered(self):
        assert is_academic("National Center for Manufacturing Sciences") is True

    def test_normal_company_still_passes(self):
        assert is_academic("Acme Technology Solutions LLC") is False
        assert is_academic("Center for Applied Engineering LLC") is False


# ─── _is_active_sbir() / _is_exact_academic_name() / _ALL_STATES ────────────


class TestIsActiveSBIR:
    def test_future_contract_end_date_always_active(self):
        assert _is_active_sbir(2015, date(2099, 1, 1)) is True

    def test_past_contract_end_date_falls_back_to_year(self):
        assert _is_active_sbir(2015, date(2015, 6, 1)) is False
        assert _is_active_sbir(2023, date(2023, 6, 1)) is True

    def test_no_contract_end_date_uses_year_only(self):
        assert _is_active_sbir(2019, None) is True
        assert _is_active_sbir(2018, None) is False


class TestIsExactAcademicName:
    def test_mit_hard_blocked(self):
        assert _is_exact_academic_name("MIT") is True

    def test_case_and_whitespace_insensitive(self):
        assert _is_exact_academic_name("  mit  ") is True

    def test_substring_not_hard_blocked(self):
        """A name merely containing 'MIT' is not exact-matched (soft-flag path instead)."""
        assert _is_exact_academic_name("MIT Spinoff Technologies LLC") is False

    def test_normal_company_not_hard_blocked(self):
        assert _is_exact_academic_name("Acme Technology Solutions LLC") is False


def test_all_states_covers_all_50():
    assert len(_ALL_STATES) >= 50
    assert "CA" in _ALL_STATES
    assert "AL" in _ALL_STATES


# ─── _normalize_bulk_row() — bulk CSV headers -> API field names ────────────


class TestNormalizeBulkRow:
    def test_maps_all_fields(self):
        row = _valid_bulk_row()
        normalized = _normalize_bulk_row(row)
        assert normalized["firm"] == "Acme Technology Solutions LLC"
        assert normalized["state"] == "Alabama"
        assert normalized["award_amount"] == "750000.0000"
        assert normalized["award_year"] == "2024"
        assert normalized["poc_name"] == "Jane Doe"
        assert normalized["poc_title"] == "CEO"
        assert normalized["poc_phone"] == "2055551234"
        assert normalized["poc_email"] == "jane@acmetech.com"
        assert normalized["company_url"] == "https://acmetech.com"
        assert normalized["number_employees"] == "12"


# ─── Bulk mode connector tests ────────────────────────────────────────────────


class TestBulkMode:
    def test_bulk_mode_downloads_cache_and_parses(self):
        _, _, source_run = _run_dual_mode(bulk_rows=[_valid_bulk_row()])
        assert source_run.status == "completed"
        assert source_run.records_valid == 1
        assert source_run.records_fetched == 1

    def test_bulk_full_state_name_normalizes(self):
        row = _valid_bulk_row(State="Georgia")
        _, _, source_run = _run_dual_mode(bulk_rows=[row])
        assert source_run.records_valid == 1

    def test_bulk_state_outside_old_icp_now_included(self):
        """California was excluded under the old 7-state ICP; the filter is removed."""
        row = _valid_bulk_row(State="California")
        _, _, source_run = _run_dual_mode(bulk_rows=[row])
        assert source_run.records_valid == 1
        assert source_run.records_skipped == 0

    def test_bulk_academic_institution_soft_flagged(self):
        row = _valid_bulk_row(Company="University of Alabama Research LLC")
        _, session, source_run = _run_dual_mode(bulk_rows=[row])
        assert source_run.records_valid == 1
        assert source_run.records_skipped == 0
        event = session.add.call_args_list[0][0][0]
        assert event.payload["sector_excluded"] is True

    def test_bulk_exact_academic_name_hard_blocked(self):
        row = _valid_bulk_row(Company="MIT")
        _, _, source_run = _run_dual_mode(bulk_rows=[row])
        assert source_run.records_valid == 0
        assert source_run.records_skipped == 1

    def test_bulk_contact_fields_stored_in_payload(self):
        _, session, source_run = _run_dual_mode(bulk_rows=[_valid_bulk_row()])
        event = session.add.call_args_list[0][0][0]
        payload = event.payload
        assert payload["poc_name"] == "Jane Doe"
        assert payload["poc_title"] == "CEO"
        assert payload["poc_phone"] == "2055551234"
        assert payload["poc_email"] == "jane@acmetech.com"
        assert payload["company_url"] == "https://acmetech.com"
        assert payload["number_employees"] == 12
        assert payload["sbir_source_mode"] == "bulk"

    def test_bulk_amount_normalized_in_payload(self):
        row = _valid_bulk_row(**{"Award Amount": "664827.0000"})
        _, session, source_run = _run_dual_mode(bulk_rows=[row])
        event = session.add.call_args_list[0][0][0]
        assert event.payload["award_amount"] == "664827"

    def test_bulk_mode_off_by_default(self):
        """SBIR_BULK_ENABLED defaults to false; only enabling API must not touch the bulk cache."""
        session = _make_session()
        source = _make_source()
        source_run = _make_source_run()
        award = _valid_award()
        with patch("app.pipeline.connectors.sbir_grants._ensure_bulk_cache") as mock_ensure_cache:
            with patch("app.pipeline.connectors.sbir_grants._fetch_page", side_effect=[[award], []]):
                connector = SBIRGrantsConnector(session, source_run, source)
                connector.run()
        mock_ensure_cache.assert_not_called()
        assert source_run.records_valid == 1


# ─── API graceful 429 handling ────────────────────────────────────────────────


class TestAPIGracefulSkip:
    def test_api_exhausted_retries_skipped_gracefully(self):
        """A ConnectorError from _fetch_page (429 exhausted) must not fail the whole run."""
        session = _make_session()
        source = _make_source()
        source_run = _make_source_run()
        with patch(
            "app.pipeline.connectors.sbir_grants._fetch_page",
            side_effect=ConnectorError("SBIR API unavailable"),
        ):
            connector = SBIRGrantsConnector(session, source_run, source)
            connector.run()
        assert source_run.status == "completed"
        assert source_run.error_text is not None
        assert "API mode skipped" in source_run.error_text

    def test_bulk_records_survive_api_failure(self):
        """If bulk mode already stored records, a subsequent API 429 must not lose them."""
        bulk_row = _valid_bulk_row()
        mock_path = MagicMock()
        mock_path.open.return_value.__enter__.return_value = io.StringIO(_csv_content([bulk_row]))
        session = _make_session()
        source = _make_source()
        source_run = _make_source_run()

        with patch("app.pipeline.connectors.sbir_grants._ensure_bulk_cache", return_value=mock_path):
            with patch(
                "app.pipeline.connectors.sbir_grants._fetch_page",
                side_effect=ConnectorError("SBIR API unavailable"),
            ):
                with patch.dict(
                    "os.environ",
                    {"SBIR_BULK_ENABLED": "true", "SBIR_API_ENABLED": "true", "SBIR_TEST_LIMIT": "0"},
                ):
                    connector = SBIRGrantsConnector(session, source_run, source)
                    connector.run()

        assert source_run.status == "completed"
        assert source_run.records_valid == 1  # bulk row stored despite API failure


# ─── Dual mode: both API and bulk run in one pass ────────────────────────────


class TestDualModeRun:
    def test_both_modes_run_when_both_enabled(self):
        bulk_row = _valid_bulk_row(Company="Bulk Co LLC")
        api_award = _valid_award(firm="API Co LLC")
        connector, session, source_run = _run_dual_mode(
            bulk_rows=[bulk_row],
            api_responses=[[api_award], []],
        )
        assert source_run.status == "completed"
        assert source_run.records_valid == 2
        modes = {c[0][0].payload["sbir_source_mode"] for c in session.add.call_args_list}
        assert modes == {"bulk", "api"}


# ─── Cross-mode dedup ─────────────────────────────────────────────────────────


class TestCrossModeDedup:
    def test_same_award_produces_same_content_hash(self):
        """A bulk row and an API row for the same award must hash identically,
        even though bulk/API rows differ in field completeness (e.g. abstract)."""
        session_bulk = _make_session()
        session_api = _make_session()
        source = _make_source()

        bulk_row = _valid_bulk_row(
            Company="Acme Tech LLC",
            State="Alabama",
            **{"Award Year": "2024", "Award Amount": "750000.0000"},
        )
        api_row = _valid_award(firm="Acme Tech LLC", state="AL", award_year=2024, award_amount=750000)

        connector_bulk = SBIRGrantsConnector(session_bulk, _make_source_run(), source)
        connector_bulk._process_record(bulk_row, mode="bulk")

        connector_api = SBIRGrantsConnector(session_api, _make_source_run(), source)
        connector_api._process_record(api_row, mode="api")

        bulk_event = session_bulk.add.call_args_list[0][0][0]
        api_event = session_api.add.call_args_list[0][0][0]
        assert bulk_event.content_hash == api_event.content_hash

    def test_second_mode_skips_when_hash_already_exists(self):
        """When the DB already has this award's identity hash (from the other mode),
        the connector must skip rather than insert a duplicate."""
        source = _make_source()
        api_row = _valid_award(firm="Acme Tech LLC", state="AL", award_year=2024, award_amount=750000)
        source_run = _make_source_run()
        session = _make_session(record_exists=True)
        connector = SBIRGrantsConnector(session, source_run, source)

        stored = connector._process_record(api_row, mode="api")

        assert stored is False
        assert source_run.records_skipped == 1
        assert source_run.records_valid == 0


# ─── Evidence extraction: contact enrichment fields ──────────────────────────


class TestSBIRContactEnrichmentEvidence:
    def _make_raw_event(self, payload: dict) -> MagicMock:
        evt = MagicMock()
        evt.id = uuid.uuid4()
        evt.source_id = uuid.uuid4()
        evt.payload = payload
        evt.source_url = "https://www.sbir.gov/awards"
        return evt

    def test_contact_fields_extracted(self):
        from app.processing.evidence import extract_evidence

        payload = {
            "sbir_signal_type": "SBIR_GRANT",
            "firm": "Acme Tech LLC",
            "state": "AL",
            "award_amount": "750000",
            "award_year": 2024,
            "poc_name": "Jane Doe",
            "poc_title": "CEO",
            "poc_phone": "2055551234",
            "poc_email": "jane@acmetech.com",
            "company_url": "https://acmetech.com",
            "number_employees": 12,
        }
        raw_event = self._make_raw_event(payload)

        db = MagicMock()
        db.get.return_value = raw_event
        db.flush = MagicMock()

        items = extract_evidence(raw_event.id, db)
        assert len(items) == 1
        item = items[0]
        assert item.extracted_fields["poc_name"] == "Jane Doe"
        assert item.extracted_fields["poc_title"] == "CEO"
        assert item.extracted_fields["poc_phone"] == "2055551234"
        assert item.extracted_fields["poc_email"] == "jane@acmetech.com"
        assert item.extracted_fields["company_url"] == "https://acmetech.com"
        assert item.extracted_fields["employee_count"] == 12

    def test_missing_contact_fields_default_none(self):
        from app.processing.evidence import extract_evidence

        payload = {
            "sbir_signal_type": "SBIR_GRANT",
            "firm": "Acme Tech LLC",
            "state": "AL",
            "award_amount": "750000",
            "award_year": 2024,
        }
        raw_event = self._make_raw_event(payload)

        db = MagicMock()
        db.get.return_value = raw_event
        db.flush = MagicMock()

        items = extract_evidence(raw_event.id, db)
        item = items[0]
        assert item.extracted_fields["poc_name"] is None
        assert item.extracted_fields["poc_phone"] is None
        assert item.extracted_fields["poc_email"] is None
        assert item.extracted_fields["employee_count"] is None


# ─── _is_government_contact() — filter agency program officers, not company staff ──


class TestIsGovernmentContact:
    def test_mil_email_flagged(self):
        assert _is_government_contact("brian.kemp@us.af.mil", None) is True

    def test_gov_email_flagged(self):
        assert _is_government_contact("someone@nasa.gov", None) is True

    def test_army_mil_flagged(self):
        assert _is_government_contact("jane.doe@army.mil", None) is True

    def test_navy_mil_flagged(self):
        assert _is_government_contact("jane.doe@navy.mil", None) is True

    def test_marines_mil_flagged(self):
        assert _is_government_contact("jane.doe@marines.mil", None) is True

    def test_uscg_mil_flagged(self):
        assert _is_government_contact("jane.doe@uscg.mil", None) is True

    def test_afwerx_flagged(self):
        assert _is_government_contact("outreach@afwerx.com", None) is True

    def test_sbir_at_prefix_flagged(self):
        assert _is_government_contact("SBIR@AFWERX.AF.MIL", None) is True

    def test_case_insensitive(self):
        assert _is_government_contact("Jane.Doe@ARMY.MIL", None) is True

    def test_placeholder_phone_flagged(self):
        assert _is_government_contact(None, "9999999999") is True

    def test_placeholder_phone_with_dashes_flagged(self):
        assert _is_government_contact(None, "999-999-9999") is True

    def test_genuine_company_contact_passes(self):
        assert _is_government_contact("jane@acmetech.com", "2055551234") is False

    def test_none_email_and_phone_passes(self):
        assert _is_government_contact(None, None) is False

    def test_empty_string_passes(self):
        assert _is_government_contact("", "") is False


# ─── Government contact filtering wired into the connector ──────────────────


class TestGovernmentContactFiltering:
    def test_bulk_government_email_nulled_but_record_still_stored(self):
        row = _valid_bulk_row(
            **{"Contact Name": "Brian Kemp", "Contact Title": "Program Manager",
               "Contact Phone": "8017755378", "Contact Email": "brian.kemp@us.af.mil"}
        )
        _, session, source_run = _run_dual_mode(bulk_rows=[row])
        assert source_run.records_valid == 1  # record itself is not skipped
        event = session.add.call_args_list[0][0][0]
        assert event.payload["poc_name"] is None
        assert event.payload["poc_title"] is None
        assert event.payload["poc_phone"] is None
        assert event.payload["poc_email"] is None
        # Non-contact fields are untouched
        assert event.payload["firm"] == row["Company"]
        assert event.payload["company_url"] == row["Company Website"]

    def test_bulk_placeholder_phone_nulled(self):
        row = _valid_bulk_row(**{"Contact Phone": "9999999999", "Contact Email": ""})
        _, session, source_run = _run_dual_mode(bulk_rows=[row])
        event = session.add.call_args_list[0][0][0]
        assert event.payload["poc_phone"] is None
        assert event.payload["poc_name"] is None

    def test_bulk_genuine_contact_preserved(self):
        row = _valid_bulk_row()  # default has a genuine company contact
        _, session, source_run = _run_dual_mode(bulk_rows=[row])
        event = session.add.call_args_list[0][0][0]
        assert event.payload["poc_name"] == "Jane Doe"
        assert event.payload["poc_phone"] == "2055551234"
        assert event.payload["poc_email"] == "jane@acmetech.com"

    def test_api_government_email_nulled(self):
        award = _valid_award(poc_name="Brian Kemp", poc_phone="8017755378", poc_email="brian.kemp@us.af.mil")
        connector, session, source_run = _run_connector([[award], []])
        event = session.add.call_args_list[0][0][0]
        assert event.payload["poc_email"] is None
        assert event.payload["poc_phone"] is None
        assert event.payload["poc_name"] is None
