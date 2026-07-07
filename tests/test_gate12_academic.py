"""
Tests for Gate 12 — Academic Institution Hard Block (app/processing/gates.py).

Confirmed by John Cox Miller, Porter Capital, July 7 2026: universities,
colleges, and medical/theological schools cannot be Porter Capital factoring
clients (they may still be the debtor on an invoice, never the client).
"""
from __future__ import annotations

import pytest

from app.processing.gates import AcademicResult, gate_12_academic_institution

# ── must FAIL (academic institutions) ─────────────────────────────────────────
ACADEMIC_NAMES = [
    "UNIVERSITY OF ALABAMA",
    "VANDERBILT UNIVERSITY",
    "BAYLOR COLLEGE OF MEDICINE",
    "MASSACHUSETTS INSTITUTE OF TECHNOLOGY",
    "VIRGINIA POLYTECHNIC INSTITUTE & STATE UNIVERSITY",
    "CINCINNATI UNIV OF",
    "ICAHN SCHOOL OF MEDICINE AT MOUNT SINAI",
    "WAKE FOREST UNIVERSITY HEALTH SCIENCES",
    "BOSTON UNIVERSITY",
    "SALK INSTITUTE FOR BIOLOGICAL STUDIES",
    "THEOLOGICAL SEMINARY OF AMERICA",
]

# ── must PASS (legitimate Porter clients) ─────────────────────────────────────
LEGITIMATE_CLIENTS = [
    "CHENEGA GLOBAL PROTECTION LLC",
    "REI SYSTEMS INC",
    "KARSUN SOLUTIONS LLC",
    "BEAST CODE LLC",
    "ASPETTO INC",
    "LBYD FEDERAL LLC",
    "INDUSTRIAL MAINTENANCE SERVICES INC",
    "APC CONSTRUCTION LLC",
]

# ── ambiguous edge cases — must PASS (be conservative; John can reject manually) ─
EDGE_CASE_PASSES = [
    "COLLEGE PARK SYSTEMS INC",
    "UNIVERSITY SERVICES LLC",
    "INSTITUTE FOR DEFENSE ANALYSES",
]


class TestAcademicNamesFail:
    @pytest.mark.parametrize("name", ACADEMIC_NAMES)
    def test_academic_institution_fails(self, name: str):
        result = gate_12_academic_institution(name)
        assert result.passed is False, f"Expected academic institution to fail gate: {name!r}"
        assert result.reason is not None
        assert name in result.reason


class TestLegitimateClientsPass:
    @pytest.mark.parametrize("name", LEGITIMATE_CLIENTS)
    def test_legitimate_client_passes(self, name: str):
        result = gate_12_academic_institution(name)
        assert result.passed is True, f"Legitimate client was unexpectedly blocked: {name!r} — {result.reason}"
        assert result.reason is None


class TestEdgeCasesPass:
    @pytest.mark.parametrize("name", EDGE_CASE_PASSES)
    def test_ambiguous_name_passes(self, name: str):
        result = gate_12_academic_institution(name)
        assert result.passed is True, f"Ambiguous name should default to PASS: {name!r} — {result.reason}"


class TestEdgeCases:
    def test_empty_string_passes(self):
        result = gate_12_academic_institution("")
        assert result.passed is True

    def test_none_passes_without_crash(self):
        result = gate_12_academic_institution(None)  # type: ignore[arg-type]
        assert result.passed is True

    def test_case_insensitive(self):
        result = gate_12_academic_institution("boston university")
        assert result.passed is False


class TestAcademicResultStructure:
    def test_pass_result_has_no_reason(self):
        result = gate_12_academic_institution("CLEAN COMPANY LLC")
        assert result.passed is True
        assert result.reason is None

    def test_fail_result_has_reason(self):
        result = gate_12_academic_institution("BOSTON UNIVERSITY")
        assert result.passed is False
        assert result.reason is not None
        assert len(result.reason) > 0

    def test_result_is_academic_result_instance(self):
        result = gate_12_academic_institution("BOSTON UNIVERSITY")
        assert isinstance(result, AcademicResult)
