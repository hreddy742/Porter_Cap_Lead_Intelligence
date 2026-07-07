"""
Tests for Gate 13 — Social Service Nonprofit Hard Block (app/processing/gates.py).

Confirmed by John Cox Miller, Porter Capital, July 2026: pure social-service /
human-services nonprofits (shelters, food banks, child day care, community
action agencies) do not generate commercial invoices to factor and are never
Porter ICP.
"""
from __future__ import annotations

import pytest

from app.processing.gates import SocialServiceResult, gate_13_social_service_nonprofit

# ── must FAIL by NAICS code ────────────────────────────────────────────────────
SOCIAL_SERVICE_NAICS_CASES = [
    ("UNITED COMMUNITY ACTION PROGRAM", "624410"),
    ("JERSEY BATTERED WOMENS SERVICE", "624221"),
    ("SOME CHILD CARE CENTER LLC", "624110"),
    ("ELDER CARE SERVICES INC", "624120"),
    ("FAMILY SUPPORT SERVICES", "624190"),
    ("MEALS ON WHEELS OF THE VALLEY", "624210"),
    ("HOUSING RELIEF NETWORK", "624229"),
    ("DISASTER RELIEF FUND INC", "624230"),
    ("VOCATIONAL REHAB SERVICES INC", "624310"),
]

# ── must FAIL by keyword, even with a non-matching or missing NAICS code ──────
SOCIAL_SERVICE_KEYWORD_CASES = [
    ("JERSEY BATTERED WOMENS SERVICE", None),
    ("VALLEY DOMESTIC VIOLENCE SHELTER", None),
    ("DOWNTOWN HOMELESS SHELTER INC", None),
    ("REGIONAL FOOD BANK", None),
    ("MAIN STREET SOUP KITCHEN", None),
    ("UNITED COMMUNITY ACTION PROGRAM", None),
    ("STATEWIDE HUMAN SERVICES AGENCY", None),
    ("COUNTY SOCIAL SERVICES DEPARTMENT", None),
]

# ── must PASS (legitimate Porter clients) ──────────────────────────────────────
LEGITIMATE_CLIENTS = [
    ("CHENEGA GLOBAL PROTECTION LLC", "541330"),
    ("REI SYSTEMS INC", "541511"),
    ("APC CONSTRUCTION LLC", "236220"),
    ("INDUSTRIAL MAINTENANCE SERVICES INC", None),
]


class TestSocialServiceNaicsFails:
    @pytest.mark.parametrize("name,naics", SOCIAL_SERVICE_NAICS_CASES)
    def test_social_service_naics_fails(self, name: str, naics: str):
        result = gate_13_social_service_nonprofit(name, naics)
        assert result.passed is False, f"Expected NAICS {naics} to fail gate: {name!r}"
        assert result.reason is not None


class TestSocialServiceKeywordFails:
    @pytest.mark.parametrize("name,naics", SOCIAL_SERVICE_KEYWORD_CASES)
    def test_social_service_keyword_fails(self, name: str, naics: str | None):
        result = gate_13_social_service_nonprofit(name, naics)
        assert result.passed is False, f"Expected keyword match to fail gate: {name!r}"
        assert result.reason is not None


class TestLegitimateClientsPass:
    @pytest.mark.parametrize("name,naics", LEGITIMATE_CLIENTS)
    def test_legitimate_client_passes(self, name: str, naics: str | None):
        result = gate_13_social_service_nonprofit(name, naics)
        assert result.passed is True, f"Legitimate client was unexpectedly blocked: {name!r} — {result.reason}"
        assert result.reason is None


class TestEdgeCases:
    def test_empty_name_and_no_naics_passes(self):
        result = gate_13_social_service_nonprofit("", None)
        assert result.passed is True

    def test_none_name_passes_without_crash(self):
        result = gate_13_social_service_nonprofit(None, None)  # type: ignore[arg-type]
        assert result.passed is True

    def test_case_insensitive_keyword_match(self):
        result = gate_13_social_service_nonprofit("battered women's shelter", None)
        assert result.passed is False

    def test_naics_prefix_longer_than_six_digits_matches_on_first_six(self):
        result = gate_13_social_service_nonprofit("SOME AGENCY LLC", "6244100")
        assert result.passed is False

    def test_unrelated_naics_prefix_passes(self):
        result = gate_13_social_service_nonprofit("SOME AGENCY LLC", "624999")
        assert result.passed is True


class TestSocialServiceResultStructure:
    def test_pass_result_has_no_reason(self):
        result = gate_13_social_service_nonprofit("CLEAN COMPANY LLC", "541511")
        assert result.passed is True
        assert result.reason is None

    def test_fail_result_has_reason(self):
        result = gate_13_social_service_nonprofit("JERSEY BATTERED WOMENS SERVICE", "624221")
        assert result.passed is False
        assert result.reason is not None
        assert len(result.reason) > 0

    def test_result_is_social_service_result_instance(self):
        result = gate_13_social_service_nonprofit("JERSEY BATTERED WOMENS SERVICE", "624221")
        assert isinstance(result, SocialServiceResult)
