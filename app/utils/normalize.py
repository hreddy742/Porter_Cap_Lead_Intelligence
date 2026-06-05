"""
Company name normalization.

THE MOST IMPORTANT FILE IN THE CODEBASE.
This function is the backbone of entity dedup, suppression matching, and external_id generation.
If it's wrong, false merges and suppression errors are silent for months.

Rules:
  - Strip ONLY true legal-entity suffixes (llc, inc, corp, ltd, etc.)
  - DO NOT strip descriptive words: group, holdings, services, solutions, enterprises,
    international, associates — these distinguish different companies
  - Normalize ampersand, punctuation, and whitespace
  - Lowercase everything
  - Output must be deterministic: same input → always same output

Usage:
    from app.utils.normalize import normalize_company_name
    key = normalize_company_name("Acme Corp LLC")  # → "acme"
    key = normalize_company_name("Acme Group LLC")  # → "acme group"  ← DIFFERENT from above
"""

import re


# TRUE legal-entity suffixes — words that indicate legal form, not business type.
# These are safe to strip because they add no business-type distinction.
# DO NOT add: group, holdings, services, solutions, enterprises, international, associates
_LEGAL_SUFFIXES = frozenset(
    {
        "llc",
        "inc",
        "corp",
        "corporation",
        "ltd",
        "limited",
        "co",
        "lp",
        "llp",
        "plc",
        "pllc",
        "pa",
        "pc",
        "na",
        "incorporated",
        "company",    # "The Acme Company" → "acme" (generic, not distinguishing)
    }
)


def normalize_company_name(name: str | None) -> str:
    """
    Produce a canonical lowercase string for dedup and external_id generation.
    Deterministic: same input always produces the same output.

    Raises ValueError if name is empty or None.

    Examples:
        "Acme Corp LLC"          → "acme"
        "ACME CORPORATION"       → "acme"
        "Acme, Inc."             → "acme"
        "Acme & Partners"        → "acme and partners"
        "Acme-Tech LLC"          → "acme tech"
        "Acme Group LLC"         → "acme group"   ← group is NOT stripped
        "Acme Services Inc"      → "acme services" ← services is NOT stripped
        "Acme Holdings Ltd"      → "acme holdings" ← holdings is NOT stripped
        "ABC Federal Services"   → "abc federal services"  ← all kept (no legal suffix)
        "ABC Federal Services Inc" → "abc federal services"  ← only Inc stripped
    """
    if not name or not str(name).strip():
        raise ValueError(f"Company name cannot be empty or None, got: {name!r}")

    s = str(name).strip().lower()

    # Normalize ampersand to "and"
    s = s.replace("&", " and ")

    # Replace punctuation with space (commas, periods, hyphens, slashes, etc.)
    s = re.sub(r"[',.\-/\\|()]", " ", s)

    # Collapse multiple whitespace into single space
    s = re.sub(r"\s+", " ", s).strip()

    # Strip trailing legal suffixes iteratively.
    # "Acme Corp LLC" → words = ["acme", "corp", "llc"]
    #   iteration 1: strip "llc" → ["acme", "corp"]
    #   iteration 2: strip "corp" → ["acme"]
    #   stop.
    words = s.split()
    while words and words[-1] in _LEGAL_SUFFIXES:
        words.pop()

    # Guard: if ALL words were suffixes (e.g. "LLC Inc Corp"), return original normalized
    # rather than returning an empty string (which would cause everything to collide)
    if not words:
        return s  # fallback to full normalized string

    return " ".join(words)


def normalize_for_suppression_key(name: str, state: str) -> str:
    """
    Produce the key used in suppression_list for name+state matching.
    Format: "{normalized_name}|{state_upper}"

    Example: normalize_for_suppression_key("Acme Corp LLC", "TX") → "acme|TX"
    """
    normalized = normalize_company_name(name)
    state_clean = (state or "").strip().upper()
    return f"{normalized}|{state_clean}"
