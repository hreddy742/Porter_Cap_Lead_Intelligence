"""
Tests for Gate 11 — OFAC SDN Screening (app/processing/gates.py).

Patches _load_ofac_sdn() for all tests that need specific SDN contents, so
no real OFAC file is required on the test machine.  One test verifies the
file-not-found path by clearing lru_cache and pointing at a non-existent path.
"""
from __future__ import annotations

import importlib
import os
from unittest.mock import patch

import pytest

from app.processing.gates import (
    OFACResult,
    _load_ofac_sdn,
    _normalize_ofac_name,
    gate_11_ofac_screening,
)

# ── normalised SDN names that represent real SDN entries ─────────────────────
_KNOWN_SDN_NAMES: frozenset[str] = frozenset(
    _normalize_ofac_name(n)
    for n in [
        "BANCO NACIONAL DE CUBA",
        "AEROCARIBBEAN AIRLINES",
        "IRAN AIR",            # alias-style test target
    ]
)

# ── Porter Capital hot leads — must never be blocked ─────────────────────────
HOT_LEADS = [
    "CAPITAL BRAND GROUP LLC",
    "VETERAN TECHNOLOGY PARTNERS LLC",
    "PROPERTY ENVIRONMENTAL MANAGEMENT INC",
    "AMENTUM TECHNOLOGY INC",
    "KIRA AEROSPACE LLC",
]


# ── helpers ───────────────────────────────────────────────────────────────────

def _patch_sdn(names: frozenset[str]):
    """Return a patch context that replaces _load_ofac_sdn with the given set."""
    return patch("app.processing.gates._load_ofac_sdn", return_value=names)


# ── normalization ─────────────────────────────────────────────────────────────

class TestNormalizeOfacName:
    def test_uppercases(self):
        assert _normalize_ofac_name("banco nacional") == "BANCO NACIONAL"

    def test_strips_llc(self):
        assert _normalize_ofac_name("Acme LLC") == "ACME"

    def test_strips_inc(self):
        assert _normalize_ofac_name("Acme Inc") == "ACME"

    def test_strips_corp(self):
        assert _normalize_ofac_name("Acme Corp") == "ACME"

    def test_keeps_international(self):
        # Descriptive words are NOT stripped — only true legal suffixes are
        assert "INTERNATIONAL" in _normalize_ofac_name("Acme International Ltd")

    def test_keeps_group(self):
        assert "GROUP" in _normalize_ofac_name("Acme Group LLC")

    def test_keeps_services(self):
        assert "SERVICES" in _normalize_ofac_name("Acme Services Inc")

    def test_removes_punctuation(self):
        result = _normalize_ofac_name("Al-Qaida, Inc.")
        assert "," not in result
        assert "-" not in result

    def test_collapses_whitespace(self):
        assert "  " not in _normalize_ofac_name("Acme   Corp  LLC")

    def test_strips_leading_trailing_whitespace(self):
        result = _normalize_ofac_name("  ACME LLC  ")
        assert result == result.strip()

    def test_cuba_variant_matches_base(self):
        # "Banco Nacional de Cuba LLC" must normalise identically to the SDN entry
        base = _normalize_ofac_name("BANCO NACIONAL DE CUBA")
        variant = _normalize_ofac_name("Banco Nacional de Cuba LLC")
        assert base == variant


# ── known OFAC entities must FAIL ─────────────────────────────────────────────

class TestKnownOfacEntitiesFail:
    def test_banco_nacional_de_cuba_fails(self):
        with _patch_sdn(_KNOWN_SDN_NAMES):
            result = gate_11_ofac_screening("BANCO NACIONAL DE CUBA")
        assert result.passed is False
        assert "BANCO NACIONAL DE CUBA" in result.reason

    def test_banco_nacional_lowercase_variant_fails(self):
        with _patch_sdn(_KNOWN_SDN_NAMES):
            result = gate_11_ofac_screening("Banco Nacional de Cuba LLC")
        assert result.passed is False

    def test_aerocaribbean_airlines_fails(self):
        with _patch_sdn(_KNOWN_SDN_NAMES):
            result = gate_11_ofac_screening("AEROCARIBBEAN AIRLINES")
        assert result.passed is False
        assert "AEROCARIBBEAN AIRLINES" in result.reason

    def test_aerocaribbean_with_suffix_fails(self):
        with _patch_sdn(_KNOWN_SDN_NAMES):
            result = gate_11_ofac_screening("AeroCaribbean Airlines Inc")
        assert result.passed is False


# ── Porter Capital hot leads must PASS ───────────────────────────────────────

class TestHotLeadsPass:
    @pytest.mark.parametrize("name", HOT_LEADS)
    def test_hot_lead_passes(self, name: str):
        with _patch_sdn(_KNOWN_SDN_NAMES):
            result = gate_11_ofac_screening(name)
        assert result.passed is True, f"Hot lead was unexpectedly blocked: {name!r} — {result.reason}"
        assert result.is_warning is False


# ── edge cases ────────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_empty_string_passes(self):
        with _patch_sdn(_KNOWN_SDN_NAMES):
            result = gate_11_ofac_screening("")
        assert result.passed is True

    def test_none_passes_without_crash(self):
        with _patch_sdn(_KNOWN_SDN_NAMES):
            result = gate_11_ofac_screening(None)  # type: ignore[arg-type]
        assert result.passed is True

    def test_unrelated_company_passes(self):
        with _patch_sdn(_KNOWN_SDN_NAMES):
            result = gate_11_ofac_screening("SMITH FABRICATION LLC")
        assert result.passed is True

    def test_file_not_found_passes_with_warning(self, tmp_path):
        _load_ofac_sdn.cache_clear()
        missing = str(tmp_path / "no_such_file.csv")
        with patch("app.processing.gates.OFAC_SDN_PATH", missing):
            result = gate_11_ofac_screening("SOME COMPANY LLC")
        _load_ofac_sdn.cache_clear()  # restore for subsequent tests
        assert result.passed is True
        assert result.is_warning is True
        assert "screening disabled" in result.reason

    def test_empty_sdn_set_passes(self):
        with _patch_sdn(frozenset()):
            result = gate_11_ofac_screening("BANCO NACIONAL DE CUBA")
        assert result.passed is True
        assert result.is_warning is True


# ── partial / alias matching ──────────────────────────────────────────────────

class TestPartialAndAliasMatching:
    def test_partial_match_blocked(self):
        # SDN name is a substring of the company name after normalisation
        # "BANCO NACIONAL DE CUBA" (22 chars ≥ 12) inside a longer company name
        company = "BANCO NACIONAL DE CUBA TRADING HOUSE"
        with _patch_sdn(_KNOWN_SDN_NAMES):
            result = gate_11_ofac_screening(company)
        assert result.passed is False
        assert "partial match" in result.reason.lower()

    def test_alias_blocked(self):
        # "IRAN AIR" is in the mock set (≥8 chars = 7... actually "IRAN AIR" is 8 chars
        # with the space. Let's use a longer alias.
        # "AEROCARIBBEAN AIRLINES" (22 chars) as a substring test
        company = "AEROCARIBBEAN AIRLINES CARGO DIVISION"
        with _patch_sdn(_KNOWN_SDN_NAMES):
            result = gate_11_ofac_screening(company)
        assert result.passed is False

    def test_short_ofac_name_not_partial_matched(self):
        # Names < 12 chars must NOT trigger partial match (floor is 12)
        # "IRAN AIR" normalises to "IRAN AIR" = 8 chars < 12 → no partial match
        short_sdn_raw = frozenset({"IRAN AIR"})  # 8 chars < 12 char floor
        company = "IRAN AIR CARGO INTERNATIONAL"
        with _patch_sdn(short_sdn_raw):
            result = gate_11_ofac_screening(company)
        # Normalised company: "IRAN AIR CARGO INTERNATIONAL"
        # Exact match: "IRAN AIR CARGO INTERNATIONAL" in {"IRAN AIR"}? No
        # Partial match: "IRAN AIR" (8 chars) < 12 threshold → not checked → passes
        assert result.passed is True

    def test_word_boundary_prevents_false_positive(self):
        # "AM LOGISTICS" (12 chars) must NOT match "BARTRAM LOGISTICS" — the
        # "AM" in "BARTRAM" is not a word boundary, so \bAM LOGISTICS\b fails.
        sdn = frozenset({"AM LOGISTICS"})
        with _patch_sdn(sdn):
            result = gate_11_ofac_screening("BARTRAM LOGISTICS LLC")
        assert result.passed is True, (
            "Word-boundary check failed: BARTRAM LOGISTICS incorrectly matched AM LOGISTICS"
        )

    def test_word_boundary_still_catches_real_match(self):
        # "AM LOGISTICS" must still block a company that actually starts with it
        sdn = frozenset({"AM LOGISTICS"})
        with _patch_sdn(sdn):
            result = gate_11_ofac_screening("AM LOGISTICS INTERNATIONAL INC")
        assert result.passed is False

    def test_exact_match_returns_correct_reason(self):
        with _patch_sdn(_KNOWN_SDN_NAMES):
            result = gate_11_ofac_screening("AEROCARIBBEAN AIRLINES")
        assert result.passed is False
        assert "exact match" in result.reason.lower()


# ── OFACResult structure ──────────────────────────────────────────────────────

class TestOFACResultStructure:
    def test_pass_result_has_no_reason(self):
        with _patch_sdn(_KNOWN_SDN_NAMES):
            result = gate_11_ofac_screening("CLEAN COMPANY LLC")
        assert result.passed is True
        assert result.reason is None
        assert result.is_warning is False

    def test_fail_result_has_reason(self):
        with _patch_sdn(_KNOWN_SDN_NAMES):
            result = gate_11_ofac_screening("BANCO NACIONAL DE CUBA")
        assert result.passed is False
        assert result.reason is not None
        assert len(result.reason) > 0


# ── per-run cache ──────────────────────────────────────────────────────────────

class TestOfacCache:
    def test_cache_hit_skips_full_check(self):
        cache: dict = {}
        with _patch_sdn(_KNOWN_SDN_NAMES), \
             patch(
                 "app.processing.gates._run_full_ofac_check",
                 side_effect=lambda name: OFACResult(passed=True),
             ) as mock_check:
            first = gate_11_ofac_screening("CAPITAL BRAND GROUP LLC", cache=cache)
            second = gate_11_ofac_screening("CAPITAL BRAND GROUP LLC", cache=cache)

        assert mock_check.call_count == 1, "second call with same name+cache must not re-run the full check"
        assert first == second

    def test_cache_miss_for_new_name(self):
        cache: dict = {}
        with _patch_sdn(_KNOWN_SDN_NAMES), \
             patch(
                 "app.processing.gates._run_full_ofac_check",
                 side_effect=lambda name: OFACResult(passed=True),
             ) as mock_check:
            gate_11_ofac_screening("CAPITAL BRAND GROUP LLC", cache=cache)
            gate_11_ofac_screening("VETERAN TECHNOLOGY PARTNERS LLC", cache=cache)

        assert mock_check.call_count == 2, "distinct names must each run the full check once"

    def test_cache_scoped_to_run_not_shared(self):
        with _patch_sdn(_KNOWN_SDN_NAMES), \
             patch(
                 "app.processing.gates._run_full_ofac_check",
                 side_effect=lambda name: OFACResult(passed=True),
             ) as mock_check:
            cache_run_1: dict = {}
            gate_11_ofac_screening("CAPITAL BRAND GROUP LLC", cache=cache_run_1)

            cache_run_2: dict = {}
            gate_11_ofac_screening("CAPITAL BRAND GROUP LLC", cache=cache_run_2)

        assert mock_check.call_count == 2, (
            "a fresh cache dict (new pipeline run) must not reuse another run's cached result"
        )

    def test_cache_none_behaves_as_before(self):
        with _patch_sdn(_KNOWN_SDN_NAMES):
            result = gate_11_ofac_screening("BANCO NACIONAL DE CUBA", cache=None)
        assert result.passed is False

    def test_cache_records_hit_and_miss_stats(self):
        cache: dict = {}
        stats = {"hits": 0, "misses": 0}
        with _patch_sdn(_KNOWN_SDN_NAMES):
            gate_11_ofac_screening("CAPITAL BRAND GROUP LLC", cache=cache, stats=stats)
            gate_11_ofac_screening("CAPITAL BRAND GROUP LLC", cache=cache, stats=stats)
            gate_11_ofac_screening("VETERAN TECHNOLOGY PARTNERS LLC", cache=cache, stats=stats)

        assert stats == {"hits": 1, "misses": 2}
