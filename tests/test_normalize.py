"""
Normalization tests.

THESE MUST ALL PASS BEFORE YOU WRITE ANY OTHER CODE.
The normalizer is the backbone of dedup, suppression, and external_id.
A bug here causes silent data corruption for months.

Run with: pytest tests/test_normalize.py -v
"""

import pytest

from app.utils.normalize import normalize_company_name, normalize_for_suppression_key


# ─── Standard cases ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    # Legal suffix stripping
    ("Acme Corp LLC",               "acme"),
    ("ACME CORPORATION",            "acme"),
    ("Acme, Inc.",                  "acme"),
    ("Acme Co.",                    "acme"),
    ("Acme Ltd",                    "acme"),
    ("Acme Limited",                "acme"),
    ("Acme LP",                     "acme"),
    ("Acme LLP",                    "acme"),
    ("Acme PLC",                    "acme"),
    ("The Acme Company",            "the acme"),   # "company" stripped, "the" kept

    # Punctuation normalization
    ("Acme & Partners",             "acme and partners"),
    ("Acme-Tech LLC",               "acme tech"),
    ("Acme/Tech Inc",               "acme tech"),
    ("Acme, Tech Inc.",             "acme tech"),

    # Whitespace normalization
    ("Acme  Corp  LLC",             "acme"),        # double spaces
    ("  Acme Corp  ",               "acme"),        # leading/trailing spaces
    ("acme corp",                   "acme"),        # already lowercase

    # Multiple suffixes
    ("Acme Corp LLC",               "acme"),        # strip both Corp and LLC

    # Mixed case
    ("ACME Corp.",                  "acme"),
    ("Acme CORP",                   "acme"),

    # No suffix — return as-is (normalized)
    ("Acme",                        "acme"),
    ("ABC Federal",                 "abc federal"),

    # Legal suffix at end only — middle suffix stays
    ("ABC Federal Services Inc",    "abc federal services"),  # only Inc stripped
])
def test_normalize_legal_suffixes(raw, expected):
    assert normalize_company_name(raw) == expected


# ─── CRITICAL: descriptive words must NOT be stripped ─────────────────────────
# These tests protect against the over-stripping bug.
# "Acme Group" and "Acme Services" are different companies — they must not collide.

@pytest.mark.parametrize("raw, expected", [
    ("Acme Group LLC",              "acme group"),       # group kept
    ("Acme Holdings Ltd",           "acme holdings"),    # holdings kept
    ("Acme Services Inc",           "acme services"),    # services kept
    ("Acme Solutions Corp",         "acme solutions"),   # solutions kept
    ("Acme Enterprises LLC",        "acme enterprises"), # enterprises kept
    ("Acme International Inc",      "acme international"), # international kept
    ("Acme Associates LLC",         "acme associates"),  # associates kept
])
def test_descriptive_words_not_stripped(raw, expected):
    """Descriptive words distinguish different companies — must not be stripped."""
    assert normalize_company_name(raw) == expected


# ─── STAYS-DISTINCT tests — the most important tests in this file ──────────────
# These prove that genuinely different companies produce different normalized names.
# A false-positive here → false merge → silent attribution/suppression corruption.

def test_group_vs_services_stay_distinct():
    """Acme Group and Acme Services are different companies."""
    a = normalize_company_name("Acme Group LLC")
    b = normalize_company_name("Acme Services Inc")
    assert a != b, f"Different companies collided: '{a}' == '{b}'"


def test_group_vs_holdings_stay_distinct():
    a = normalize_company_name("Acme Group LLC")
    b = normalize_company_name("Acme Holdings Ltd")
    assert a != b, f"Different companies collided: '{a}' == '{b}'"


def test_abc_federal_services_vs_solutions_stay_distinct():
    a = normalize_company_name("ABC Federal Services Inc")
    b = normalize_company_name("ABC Federal Solutions LLC")
    assert a != b, f"Different companies collided: '{a}' == '{b}'"


def test_plain_acme_vs_acme_group_stay_distinct():
    """'Acme' and 'Acme Group' must not normalize to the same string."""
    a = normalize_company_name("Acme Inc")
    b = normalize_company_name("Acme Group LLC")
    assert a != b, f"'{a}' should not equal '{b}'"


# ─── Idempotency ───────────────────────────────────────────────────────────────

def test_normalize_is_idempotent():
    """Running normalize twice produces the same result."""
    raw = "Acme Tech Services LLC"
    first = normalize_company_name(raw)
    second = normalize_company_name(first)
    assert first == second, "normalize is not idempotent"


def test_different_casings_produce_same_output():
    assert normalize_company_name("acme corp") == normalize_company_name("ACME Corp.")
    assert normalize_company_name("acme group") == normalize_company_name("ACME GROUP LLC")


# ─── Error cases ───────────────────────────────────────────────────────────────

def test_empty_string_raises():
    with pytest.raises(ValueError):
        normalize_company_name("")


def test_whitespace_only_raises():
    with pytest.raises(ValueError):
        normalize_company_name("   ")


def test_none_raises():
    with pytest.raises(ValueError):
        normalize_company_name(None)


def test_all_suffix_string_does_not_return_empty():
    """If every word is a legal suffix, return the full normalized string rather than ''."""
    result = normalize_company_name("LLC Inc Corp")
    # Should not be empty — fallback to full normalized string
    assert result != "", "All-suffix input should not return empty string"
    assert len(result) > 0


# ─── Suppression key ───────────────────────────────────────────────────────────

def test_suppression_key_format():
    key = normalize_for_suppression_key("Acme Corp LLC", "TX")
    assert key == "acme|TX"


def test_suppression_key_state_normalized():
    key = normalize_for_suppression_key("Acme Corp", "tx")
    assert key.endswith("|TX"), f"State not uppercased: {key}"


def test_suppression_key_empty_state():
    key = normalize_for_suppression_key("Acme Corp", "")
    assert "|" in key
