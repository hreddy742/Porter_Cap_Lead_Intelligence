"""
Tests for app/processing/evidence.py — extract_evidence().

All tests use mocked sessions (MagicMock). No real database required.

Freshness formula: max(0.0, round(1.0 - (days_old / 180), 4))
  0 days old  → 1.0
  90 days old → 0.5
  180 days old → 0.0
  200 days old → 0.0  (clamped, never negative)
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.processing.evidence import extract_evidence


# ─── Shared helpers ───────────────────────────────────────────────────────────


def _make_raw_event(
    *,
    payload: dict | None = None,
    source_url: str = "https://www.usaspending.gov/award/CONT_AWD_12345/",
) -> MagicMock:
    event = MagicMock()
    event.id = uuid.uuid4()
    event.source_id = uuid.uuid4()
    event.payload = payload if payload is not None else _default_payload()
    event.source_url = source_url
    return event


def _default_payload(*, action_date: str | None = None) -> dict:
    return {
        "Award ID": "CONT_AWD_12345",
        "Recipient Name": "Acme Federal Services",
        "Award Amount": 500_000.0,
        "Start Date": action_date or date.today().isoformat(),
        "Recipient UEI": "UEI123456789",
        "NAICS Code": "541511",
        "NAICS Description": "Custom Computer Programming Services",
        "Place of Performance State Code": "VA",
        "Award Type": "DEFINITIVE CONTRACT",
        "Awarding Agency": "Dept of Defense",
    }


def _make_session(*, raw_event=None) -> MagicMock:
    session = MagicMock()
    session.get.return_value = raw_event
    return session


# ─── Test 1: valid payload → correct evidence item ────────────────────────────


def test_valid_payload_creates_evidence_item():
    """A valid USASpending payload produces one EvidenceItem with the right fields."""
    raw_event = _make_raw_event()
    session = _make_session(raw_event=raw_event)

    result = extract_evidence(raw_event.id, session)

    assert len(result) == 1, "should return exactly one evidence item"
    item = result[0]

    assert item.claim_supported == "CONTRACT_AWARD"
    assert item.confidence_score == Decimal("0.9")
    assert item.source_url == "https://www.usaspending.gov/award/CONT_AWD_12345/"
    assert item.company_id is None, "company_id must be NULL until resolution"
    assert item.raw_event_id == raw_event.id
    assert item.source_id == raw_event.source_id

    session.add.assert_called_once_with(item)


# ─── Test 2: freshness scores ─────────────────────────────────────────────────


@pytest.mark.parametrize("days_old, expected_freshness", [
    (0, 1.0),
    (90, 0.5),
    (180, 0.0),
    (200, 0.0),
])
def test_freshness_score_formula(days_old, expected_freshness):
    """freshness = max(0, round(1.0 - days_old/180, 4)); never goes negative."""
    action_date = date.today() - timedelta(days=days_old)
    payload = _default_payload(action_date=action_date.isoformat())
    raw_event = _make_raw_event(payload=payload)
    session = _make_session(raw_event=raw_event)

    result = extract_evidence(raw_event.id, session)

    assert len(result) == 1
    freshness = float(result[0].freshness_score)
    assert freshness == pytest.approx(expected_freshness, abs=1e-4)
    assert freshness >= 0.0, "freshness must never be negative"


# ─── Test 3: missing action_date → quarantine ─────────────────────────────────


def test_missing_action_date_returns_empty_list():
    """A payload without Start Date is quarantined: returns [], raises nothing."""
    payload = _default_payload()
    del payload["Start Date"]
    raw_event = _make_raw_event(payload=payload)
    session = _make_session(raw_event=raw_event)

    result = extract_evidence(raw_event.id, session)

    assert result == [], "missing action_date must quarantine the record"
    session.add.assert_not_called()


# ─── Test 4: missing company_name → quarantine ────────────────────────────────


def test_missing_company_name_returns_empty_list():
    """A payload without Recipient Name is quarantined: returns [], raises nothing."""
    payload = _default_payload()
    del payload["Recipient Name"]
    raw_event = _make_raw_event(payload=payload)
    session = _make_session(raw_event=raw_event)

    result = extract_evidence(raw_event.id, session)

    assert result == [], "missing company_name must quarantine the record"
    session.add.assert_not_called()


# ─── Test 5: extracted_fields contains all 9 required keys ───────────────────


def test_extracted_fields_contains_all_required_keys():
    """extracted_fields JSONB must contain exactly the 9 required keys."""
    REQUIRED_KEYS = {
        "company_name",
        "uei",
        "award_amount",
        "naics_code",
        "naics_description",
        "action_date",
        "state_code",
        "award_type",
        "awarding_agency",
    }
    raw_event = _make_raw_event()
    session = _make_session(raw_event=raw_event)

    result = extract_evidence(raw_event.id, session)

    assert len(result) == 1
    fields = result[0].extracted_fields
    missing = REQUIRED_KEYS - set(fields.keys())
    assert not missing, f"extracted_fields missing required keys: {missing}"


# ─── Test 6: source_url is never None or empty ───────────────────────────────


def test_source_url_is_never_none_or_empty():
    """A valid USASpending payload always produces a non-empty, real source_url."""
    raw_event = _make_raw_event()
    session = _make_session(raw_event=raw_event)

    result = extract_evidence(raw_event.id, session)

    assert len(result) == 1
    source_url = result[0].source_url
    assert source_url is not None, "source_url must not be None"
    assert source_url != "", "source_url must not be empty"
    assert "usaspending.gov" in source_url, "source_url must point to USASpending"
    assert "CONT_AWD_12345" in source_url, "source_url must contain the award ID"


# ─── Tests 7–10: NAICS field-name normalization ───────────────────────────────


def test_naics_code_from_capitalized_payload_key():
    """Payload 'NAICS Code' (capitalized API form) maps to extracted_fields['naics_code']."""
    payload = _default_payload()
    payload["NAICS Code"] = "541511"
    raw_event = _make_raw_event(payload=payload)
    session = _make_session(raw_event=raw_event)

    result = extract_evidence(raw_event.id, session)

    assert len(result) == 1
    assert result[0].extracted_fields["naics_code"] == "541511"


def test_naics_code_from_lowercase_payload_key():
    """Payload 'naics_code' (lowercase fallback) maps to extracted_fields['naics_code']."""
    payload = _default_payload()
    del payload["NAICS Code"]           # remove capitalized form
    payload["naics_code"] = "541511"    # only lowercase present
    raw_event = _make_raw_event(payload=payload)
    session = _make_session(raw_event=raw_event)

    result = extract_evidence(raw_event.id, session)

    assert len(result) == 1
    assert result[0].extracted_fields["naics_code"] == "541511"


def test_naics_description_from_capitalized_payload_key():
    """Payload 'NAICS Description' (capitalized) maps to extracted_fields['naics_description']."""
    payload = _default_payload()
    payload["NAICS Description"] = "Custom Computer Programming Services"
    raw_event = _make_raw_event(payload=payload)
    session = _make_session(raw_event=raw_event)

    result = extract_evidence(raw_event.id, session)

    assert len(result) == 1
    assert result[0].extracted_fields["naics_description"] == "Custom Computer Programming Services"


def test_naics_description_from_lowercase_payload_key():
    """Payload 'naics_description' (lowercase fallback) maps to extracted_fields['naics_description']."""
    payload = _default_payload()
    del payload["NAICS Description"]                                    # remove capitalized form
    payload["naics_description"] = "Custom Computer Programming Services"  # only lowercase present
    raw_event = _make_raw_event(payload=payload)
    session = _make_session(raw_event=raw_event)

    result = extract_evidence(raw_event.id, session)

    assert len(result) == 1
    assert result[0].extracted_fields["naics_description"] == "Custom Computer Programming Services"


# ─── Tests 11–13: source_url deep-link behavior ───────────────────────────────


def test_source_url_uses_generated_internal_id_when_present():
    """When generated_internal_id is in the payload, source_url must use it."""
    payload = _default_payload()
    payload["generated_internal_id"] = "CONT_AWD_12345_9700_-NONE-_-NONE-"
    raw_event = _make_raw_event(payload=payload)
    session = _make_session(raw_event=raw_event)

    result = extract_evidence(raw_event.id, session)

    assert len(result) == 1
    assert "CONT_AWD_12345_9700_-NONE-_-NONE-" in result[0].source_url, (
        "source_url must embed generated_internal_id when present"
    )


def test_source_url_is_not_homepage():
    """source_url must never be the generic USASpending homepage."""
    raw_event = _make_raw_event()
    session = _make_session(raw_event=raw_event)

    result = extract_evidence(raw_event.id, session)

    assert len(result) == 1
    url = result[0].source_url
    assert url.strip("/") != "https://www.usaspending.gov", (
        "source_url must not be the generic homepage"
    )
    assert "usaspending.gov/award/" in url, (
        "source_url must be an award detail path"
    )


def test_source_url_falls_back_to_raw_event_source_url_when_no_award_id():
    """When the payload has neither generated_internal_id nor Award ID, fall back to raw_event.source_url."""
    payload = _default_payload()
    del payload["Award ID"]
    fallback = "https://www.usaspending.gov/award/CONT_AWD_FALLBACK_9700_-NONE-_-NONE-/"
    raw_event = _make_raw_event(payload=payload, source_url=fallback)
    session = _make_session(raw_event=raw_event)

    result = extract_evidence(raw_event.id, session)

    assert len(result) == 1
    assert result[0].source_url == fallback, (
        "source_url must fall back to raw_event.source_url when payload has no identifier"
    )
