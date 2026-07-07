"""
Mandatory gates — runs BEFORE point scoring (founding rule 4).

evaluate_mandatory_gates() checks all structural and business-rule preconditions
that must hold before a company can enter the scoring queue.

A gated result never receives a numeric score.
This module does NOT calculate scores, create lead_scores, run the scoring
engine, push to Salesforce, or delete anything.
"""
from __future__ import annotations

import csv
import os
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import NamedTuple
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Company, EvidenceItem, LeadCandidate, Signal
from app.processing.suppression import check_suppression

logger = structlog.get_logger(__name__)

OFAC_SDN_PATH = os.getenv(
    "OFAC_SDN_PATH",
    "data/ofac_sdn.csv",  # relative to CWD; override via OFAC_SDN_PATH env var
)


class OFACResult(NamedTuple):
    passed: bool
    reason: str | None = None
    is_warning: bool = False


def _normalize_ofac_name(name: str) -> str:
    """Uppercase, strip punctuation, strip TRUE legal suffixes only, collapse whitespace.

    Only strips legal entity designators (LLC, INC, CORP, etc.) — NOT descriptive words
    like GROUP, GLOBAL, SERVICES.  Stripping descriptive words collapses real company
    names to single generic tokens (e.g. "GLOBAL TECHNOLOGY CORP" → "TECHNOLOGY")
    causing false-positive matches on legitimate companies.
    """
    name = name.upper()
    name = re.sub(r'[^\w\s]', ' ', name)
    for suffix in (
        'LLC', 'INC', 'CORP', 'LTD', 'CO',
        'COMPANY', 'CORPORATION', 'LIMITED',
        'LP', 'LLP', 'PLC',
    ):
        name = re.sub(rf'\b{suffix}\b', '', name)
    return re.sub(r'\s+', ' ', name).strip()


@lru_cache(maxsize=1)
def _load_ofac_sdn() -> frozenset[str]:
    """
    Load OFAC SDN list into a frozenset of normalized names.
    File format (no header):  col 0 = SDN ID, col 1 = entity name, col 11 = remarks.
    Aliases in remarks are extracted from a.k.a. "NAME" patterns.
    Returns empty frozenset if file not found — gate is disabled, not crashed.
    """
    names: set[str] = set()
    try:
        with open(OFAC_SDN_PATH, encoding='utf-8', errors='replace', newline='') as f:
            reader = csv.reader(f)
            for row in reader:
                if len(row) > 1 and row[1] and row[1] != '-0-':
                    names.add(_normalize_ofac_name(row[1]))
                if len(row) > 11 and row[11] and row[11] != '-0-':
                    for alias in re.findall(
                        r'a\.k\.a\.\s*["\']([^"\']+)["\']',
                        row[11],
                        re.IGNORECASE,
                    ):
                        if alias and alias != '-0-':
                            names.add(_normalize_ofac_name(alias))
        logger.info("ofac_sdn_loaded", total_names=len(names), path=OFAC_SDN_PATH)
        return frozenset(names)
    except FileNotFoundError:
        logger.error(
            "ofac_file_not_found",
            path=OFAC_SDN_PATH,
            note="Gate 11 OFAC screening DISABLED — set OFAC_SDN_PATH to re-enable",
        )
        return frozenset()


def gate_11_ofac_screening(company_name: str) -> OFACResult:
    """
    Gate 11 — OFAC SDN Screening (hard block).

    Checks company name against the US Treasury Office of Foreign Assets Control
    Specially Designated Nationals list.  A match means Porter Capital is legally
    prohibited from doing business with this entity.

    Returns FAIL if match found (exact or partial ≥ 8 chars).
    Returns PASS with is_warning=True if SDN file not found (gate disabled).
    Confirmed by John Cox Miller — Porter Capital compliance requirement, June 2026.
    """
    if not company_name:
        return OFACResult(passed=True)

    ofac_names = _load_ofac_sdn()

    if not ofac_names:
        return OFACResult(
            passed=True,
            reason="OFAC file not found — screening disabled",
            is_warning=True,
        )

    normalized = _normalize_ofac_name(company_name)

    if normalized in ofac_names:
        return OFACResult(passed=False, reason=f"OFAC SDN exact match: {company_name}")

    for ofac_name in ofac_names:
        # Word-boundary regex prevents "AM LOGISTICS" from matching inside "BARTRAM LOGISTICS"
        if len(ofac_name) >= 12 and re.search(
            rf'\b{re.escape(ofac_name)}\b', normalized
        ):
            return OFACResult(
                passed=False,
                reason=f"OFAC SDN partial match: {ofac_name} in {company_name}",
            )

    return OFACResult(passed=True)


class AcademicResult(NamedTuple):
    passed: bool
    reason: str | None = None


# Multi-word phrases that unambiguously identify an academic institution
# wherever they appear in the name. Bare single words like "college" or
# "university" are deliberately excluded here — "COLLEGE PARK SYSTEMS INC"
# and "UNIVERSITY SERVICES LLC" are real, non-academic company names, so a
# blanket substring match on those words would misclassify legitimate leads.
_ACADEMIC_NAME_KEYWORDS = frozenset({
    "univeristy",  # common typo — always academic if present
    "university of",
    "college of",
    "univ of",
    "institute of technology",
    "polytechnic institute",
    "school of medicine",
    "school of public health",
    "school of nursing",
    "medical school",
    "health sciences",
    "community college",
    "graduate school",
    "theological seminary",
    "divinity school",
    "seminary",
})

# "university"/"college"/etc. as the last word of the name is unambiguous
# ("VANDERBILT UNIVERSITY", "BOSTON UNIVERSITY").
_ACADEMIC_NAME_SUFFIXES = ("university", "college", "institute", "academy", "seminary")

# Research institutes named "<Name> Institute for ... Studies" are degree-granting
# academic bodies (e.g. Salk Institute for Biological Studies), distinct from
# non-academic policy institutes like "Institute for Defense Analyses".
_INSTITUTE_STUDIES_PATTERN = re.compile(r"\binstitute\b.*\bstudies\b")


def gate_12_academic_institution(company_name: str) -> AcademicResult:
    """
    Gate 12 — Academic Institution Hard Block.

    Universities, colleges, and medical/theological schools cannot be Porter
    Capital factoring clients (they may still be the debtor on an invoice,
    just never the client submitting invoices).

    Returns FAIL if company_name matches an academic-institution keyword, suffix,
    or the "institute ... studies" research-institute pattern.
    Ambiguous names (e.g. "Institute for Defense Analyses") default to PASS —
    a human reviewer can always reject manually via the review UI.
    Confirmed by John Cox Miller — Porter Capital compliance requirement, July 7 2026.
    """
    if not company_name:
        return AcademicResult(passed=True)

    name_lower = company_name.lower().strip()

    for keyword in _ACADEMIC_NAME_KEYWORDS:
        if keyword in name_lower:
            return AcademicResult(
                passed=False, reason=f"Academic institution: {company_name}"
            )

    for suffix in _ACADEMIC_NAME_SUFFIXES:
        if name_lower.endswith(suffix):
            return AcademicResult(
                passed=False,
                reason=f"Academic institution suffix: {company_name}",
            )

    if _INSTITUTE_STUDIES_PATTERN.search(name_lower):
        return AcademicResult(
            passed=False,
            reason=f"Academic research institute: {company_name}",
        )

    return AcademicResult(passed=True)


# Prefixes for the soft-flag (Phase 2B ICP policy, confirmed by John Cox Miller June 24 2026).
# Leads in these sectors are scored and stored normally but hidden from sales by default.
_EXCLUDED_NAICS_PREFIXES: frozenset[str] = frozenset({
    "11",  # Agriculture
    "22",  # Utilities
    "23",  # Construction
    "52",  # Finance and Insurance
    "61",  # Educational Services
    "62",  # Health Care and Social Assistance
    "71",  # Arts, Entertainment, Recreation
    "92",  # Public Administration
    # Source: John Cox Miller, Porter Capital, June 24 2026
})

_EXCLUDED_INDUSTRY_KEYWORDS = frozenset({"finance", "bank", "lender", "education", "government"})
_SOFT_BLOCK_ROUTES = frozenset({
    "account_review",
    "existing_lead_review",
    "duplicate_review",
    "research_review",
})


def _parse_env_decimal(name: str, default: Decimal) -> Decimal:
    """Read an env var and parse it as Decimal.

    Returns default if the variable is unset.
    Raises ValueError with a clear message if the value is set but not a valid decimal,
    so misconfiguration is caught at call time rather than silently producing wrong results.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return Decimal(raw.strip())
    except (InvalidOperation, ValueError):
        raise ValueError(
            f"Configuration error: {name}={raw!r} is not a valid decimal number. "
            f"Fix the environment variable or unset it to use the default ({default})."
        )


def _parse_signal_amount(value) -> Decimal | None:
    """Return value as a positive Decimal, or None if non-positive or unparseable."""
    if value is None:
        return None
    try:
        d = Decimal(str(value))
        return d if d > 0 else None
    except (InvalidOperation, ValueError):
        return None


def _gated(gate_reason: str, route: str) -> dict:
    return {
        "passed": False,
        "gate_name": gate_reason,
        "gate_reason": gate_reason,
        "route": route,
        "should_score": False,
        "suppression": None,
    }


def _is_excluded_industry(company: Company) -> bool:
    industry = (company.industry or "").lower()
    return any(kw in industry for kw in _EXCLUDED_INDUSTRY_KEYWORDS)


def evaluate_mandatory_gates(company_id: UUID, db: Session) -> dict:
    """
    Run all mandatory gates for company_id.

    Returns a dict with keys:
        passed        bool
        gate_name     str | None   — which gate fired (None if all passed)
        gate_reason   str | None   — machine-readable reason code
        route         str | None   — where this company should go next
        should_score  bool         — always False when passed=False
        suppression   dict | None  — raw check_suppression result (None for pre-suppression gates)

    Gates run in order; first failure wins.
    """
    company: Company | None = db.get(Company, company_id)
    if company is None:
        return _gated("company_not_found", "archive")

    # Gate 1 — no evidence
    evidence_items = (
        db.execute(select(EvidenceItem).where(EvidenceItem.company_id == company_id))
        .scalars()
        .all()
    )
    if not evidence_items:
        return _gated("no_evidence", "archive")

    # Gate 2 — no signal
    signals = (
        db.execute(select(Signal).where(Signal.company_id == company_id))
        .scalars()
        .all()
    )
    if not signals:
        return _gated("no_signal", "archive")

    # Gate 3 — stale signal (every signal is below the freshness floor)
    if all(float(s.freshness_score) < 0.1 for s in signals):
        return _gated("stale_signal", "archive")

    # Gate 4 — excluded industry
    if _is_excluded_industry(company):
        return _gated("excluded_industry", "archive")

    # Gate 5 — non-US company
    if company.country and company.country != "US":
        return _gated("non_us", "archive")

    # Gate 6 — clearly B2C
    if company.business_type == "b2c":
        return _gated("b2c", "archive")

    # Gates 7 & 8 — suppression (hard block first, then soft routes)
    suppression = check_suppression(company_id, db)

    if suppression["route"] == "hard_block":
        return {
            "passed": False,
            "gate_name": "suppression_hard_block",
            "gate_reason": suppression["reason"],
            "route": "hard_block",
            "should_score": False,
            "suppression": suppression,
        }

    if suppression["route"] in _SOFT_BLOCK_ROUTES:
        return {
            "passed": False,
            "gate_name": "suppression_route",
            "gate_reason": suppression["reason"],
            "route": suppression["route"],
            "should_score": False,
            "suppression": suppression,
        }

    # Gate 9 — duplicate active lead candidate already exists for this company
    active_leads = (
        db.execute(
            select(LeadCandidate).where(
                LeadCandidate.company_id == company_id,
                LeadCandidate.status == "active",
                LeadCandidate.deleted_at.is_(None),
            )
        )
        .scalars()
        .all()
    )
    if active_leads:
        return {
            "passed": False,
            "gate_name": "duplicate_active",
            "gate_reason": "duplicate_active",
            "route": "existing_lead_review",
            "should_score": False,
            "suppression": suppression,
        }

    # Gate 10 — award amount quality gate
    # Only positive award amounts count. A company passes if:
    #   (a) its largest single positive award >= MIN_QUALIFYING_SINGLE_AWARD_AMOUNT, OR
    #   (b) its total positive awards in the last 90 days >= MIN_QUALIFYING_COMPANY_90D_AWARD_TOTAL.
    # Negative and zero amounts never contribute to qualification.
    _min_single = _parse_env_decimal(
        "MIN_QUALIFYING_SINGLE_AWARD_AMOUNT", Decimal("10000")
    )
    _min_90d = _parse_env_decimal(
        "MIN_QUALIFYING_COMPANY_90D_AWARD_TOTAL", Decimal("10000")
    )
    cutoff = date.today() - timedelta(days=90)
    largest_single = Decimal("0")
    recent_total = Decimal("0")
    for s in signals:
        amt = _parse_signal_amount(s.award_amount)
        if amt is None:
            continue
        if amt > largest_single:
            largest_single = amt
        if isinstance(s.signal_date, date) and s.signal_date >= cutoff:
            recent_total += amt
    if largest_single < _min_single and recent_total < _min_90d:
        return _gated("award_amount_too_small", "archive")

    # Gate 11 — OFAC SDN screening (hard block)
    ofac = gate_11_ofac_screening(company.canonical_name)
    if not ofac.passed:
        logger.warning(
            "gate_11_ofac_match",
            company=company.canonical_name,
            reason=ofac.reason,
        )
        return {
            "passed": False,
            "gate_name": "ofac_sdn_match",
            "gate_reason": ofac.reason or "ofac_sdn_match",
            "route": "hard_block",
            "should_score": False,
            "suppression": suppression,
        }

    # Gate 12 — academic institution screening (hard block)
    academic = gate_12_academic_institution(company.canonical_name)
    if not academic.passed:
        logger.warning(
            "gate_12_academic_institution",
            company=company.canonical_name,
            reason=academic.reason,
        )
        return {
            "passed": False,
            "gate_name": "academic_institution",
            "gate_reason": academic.reason or "academic_institution",
            "route": "hard_block",
            "should_score": False,
            "suppression": suppression,
        }

    # All gates passed — company may proceed to scoring
    return {
        "passed": True,
        "gate_name": None,
        "gate_reason": None,
        "route": "score",
        "should_score": True,
        "suppression": suppression,
    }


def flag_excluded_sector(
    company: Company,
    lead_candidate: LeadCandidate,
    db: Session,
) -> bool:
    """
    Soft-flags a lead if its NAICS falls in an excluded sector per Porter ICP policy.

    Does NOT block the lead. Lead still gets scored and stored normally.
    Returns True if flagged, False if not.
    Only flags if company.naics_code is not None.
    """
    if not company.naics_code:
        return False
    for prefix in _EXCLUDED_NAICS_PREFIXES:
        if company.naics_code.startswith(prefix):
            lead_candidate.sector_excluded = True
            lead_candidate.sector_excluded_reason = (
                f"NAICS {company.naics_code} is in excluded "
                f"sector {prefix} per Porter ICP policy "
                f"confirmed by John Cox Miller June 24 2026"
            )
            db.flush()
            logger.info(
                "lead_sector_excluded_flagged",
                company=company.canonical_name,
                naics=company.naics_code,
                prefix=prefix,
            )
            return True
    return False
