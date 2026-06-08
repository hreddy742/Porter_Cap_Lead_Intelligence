"""
Tests for app/processing/resolution.py — resolve_company_for_evidence().

All tests use mocked sessions. No real database required.

Hard-ID priority: UEI → domain → state_entity_id
Fuzzy threshold: 0.85 (SequenceMatcher.ratio)
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest

from app.db.models import Company, CompanyIdentifier, DuplicateReview, EvidenceItem
from app.processing.resolution import resolve_company_for_evidence


# ─── Shared helpers ───────────────────────────────────────────────────────────


def _make_evidence(
    *,
    company_name: str | None = "Acme Federal Services",
    uei: str | None = None,
    domain: str | None = None,
    state_entity_id: str | None = None,
    state_code: str = "VA",
    naics_code: str | None = None,
    naics_description: str | None = None,
) -> MagicMock:
    ev = MagicMock(spec=EvidenceItem)
    ev.id = uuid.uuid4()
    ev.company_id = None
    ev.extracted_fields = {
        "company_name": company_name,
        "uei": uei,
        "domain": domain,
        "state_entity_id": state_entity_id,
        "state_code": state_code,
        "naics_code": naics_code,
        "naics_description": naics_description,
    }
    return ev


def _make_session(
    *,
    evidence: MagicMock,
    identifier=None,
    company: Company | None = None,
    fuzzy_companies: list[Company] | None = None,
    existing_duplicate=None,
) -> MagicMock:
    """
    Build a mock session for resolution tests.

    execute() dispatches by table name in the generated SQL string:
      "company_identifiers" → scalar_one_or_none returns `identifier`
      "duplicate_review"    → scalar_one_or_none returns `existing_duplicate`
      anything else         → scalars().all() returns `fuzzy_companies`

    add() captures every object and immediately assigns a UUID id if none is set,
    simulating what a real flush does for PK defaults.
    """
    added: list = []

    def _add(obj):
        added.append(obj)
        try:
            if obj.id is None:
                obj.id = uuid.uuid4()
        except Exception:
            pass

    def _get(cls, pk):
        if cls is EvidenceItem:
            return evidence
        if cls is Company:
            return company
        return None

    def _execute(stmt):
        stmt_str = str(stmt)
        result = MagicMock()
        if "company_identifiers" in stmt_str:
            result.scalar_one_or_none.return_value = identifier
        elif "duplicate_review" in stmt_str:
            result.scalar_one_or_none.return_value = existing_duplicate
        else:
            result.scalars.return_value.all.return_value = fuzzy_companies or []
        return result

    s = MagicMock()
    s.get.side_effect = _get
    s.add.side_effect = _add
    s.execute.side_effect = _execute
    s._added = added
    return s


# ─── Test 1: new UEI → one company + one identifier ──────────────────────────


def test_new_uei_creates_company_and_identifier():
    """Evidence with a new UEI creates exactly one Company and one CompanyIdentifier."""
    ev = _make_evidence(uei="UEI123456789")
    db = _make_session(evidence=ev)

    result = resolve_company_for_evidence(ev.id, db)

    assert result is not None

    companies = [o for o in db._added if isinstance(o, Company)]
    assert len(companies) == 1, "must create exactly one Company"

    identifiers = [o for o in db._added if isinstance(o, CompanyIdentifier)]
    assert len(identifiers) == 1, "must create exactly one CompanyIdentifier"
    assert identifiers[0].id_type == "uei"
    assert identifiers[0].id_value == "UEI123456789"


# ─── Test 2: same UEI → attach to existing, no duplicate ─────────────────────


def test_same_uei_attaches_to_existing_company():
    """Second evidence with the same UEI links to the existing company — no new Company row."""
    existing_company = Company(
        id=uuid.uuid4(),
        canonical_name="Acme Federal Services",
        normalized_name="acme federal services",
        state="VA",
    )
    identifier_mock = MagicMock()
    identifier_mock.company_id = existing_company.id

    ev = _make_evidence(uei="UEI123456789")
    db = _make_session(
        evidence=ev,
        identifier=identifier_mock,
        company=existing_company,
    )

    result = resolve_company_for_evidence(ev.id, db)

    assert result is existing_company
    assert ev.company_id == existing_company.id

    new_companies = [o for o in db._added if isinstance(o, Company)]
    assert len(new_companies) == 0, "must NOT create a new Company when UEI matches"


# ─── Test 3: no hard ID → new company, no identifier ─────────────────────────


def test_no_hard_id_creates_new_company():
    """Evidence with no UEI/domain/state_entity_id creates a new company with no identifiers."""
    ev = _make_evidence()  # no hard IDs
    db = _make_session(evidence=ev)

    result = resolve_company_for_evidence(ev.id, db)

    assert result is not None
    companies = [o for o in db._added if isinstance(o, Company)]
    assert len(companies) == 1
    assert companies[0].canonical_name == "Acme Federal Services"

    identifiers = [o for o in db._added if isinstance(o, CompanyIdentifier)]
    assert len(identifiers) == 0, "no hard IDs → no CompanyIdentifier rows"


# ─── Test 4: fuzzy similar names → two companies + one duplicate_review ───────


def test_fuzzy_similar_names_create_two_companies_and_one_duplicate_review():
    """
    Two companies with name similarity > 0.85 and no hard ID match:
      - each gets its own Company row (never merged)
      - a DuplicateReview row is created for human review

    "quality control systems" vs "quality control system" → ratio ≈ 0.978
    """
    # First call: creates Company A with no existing companies
    ev_a = _make_evidence(company_name="Quality Control Systems")
    db_a = _make_session(evidence=ev_a, fuzzy_companies=[])
    company_a = resolve_company_for_evidence(ev_a.id, db_a)

    assert company_a is not None
    assert company_a.normalized_name == "quality control systems"

    # Second call: creates Company B, fuzzy search returns Company A
    ev_b = _make_evidence(company_name="Quality Control System")
    db_b = _make_session(evidence=ev_b, fuzzy_companies=[company_a])
    company_b = resolve_company_for_evidence(ev_b.id, db_b)

    assert company_b is not None
    assert company_b is not company_a, "must produce two separate Company rows"

    dup_reviews = [o for o in db_b._added if isinstance(o, DuplicateReview)]
    assert len(dup_reviews) == 1, "must create exactly one DuplicateReview"
    assert dup_reviews[0].match_basis == "name_similarity"
    assert float(dup_reviews[0].similarity_score) > _FUZZY_THRESHOLD_FOR_TEST


_FUZZY_THRESHOLD_FOR_TEST = 0.85  # mirrors resolution._FUZZY_THRESHOLD


# ─── Test 5: deliberate false-merge guard ─────────────────────────────────────


def test_acme_tech_and_acme_technology_never_merge():
    """
    Deliberate false-merge guard:
      "Acme Tech LLC"       → normalized "acme tech"
      "Acme Technology Inc" → normalized "acme technology"
      similarity ≈ 0.75, below 0.85 → must produce two companies, zero duplicate_review
    """
    ev_a = _make_evidence(company_name="Acme Tech LLC")
    db_a = _make_session(evidence=ev_a, fuzzy_companies=[])
    company_a = resolve_company_for_evidence(ev_a.id, db_a)

    assert company_a is not None
    assert company_a.normalized_name == "acme tech"

    ev_b = _make_evidence(company_name="Acme Technology Inc")
    # Fuzzy search would return company_a, but similarity 0.75 < 0.85
    db_b = _make_session(evidence=ev_b, fuzzy_companies=[company_a])
    company_b = resolve_company_for_evidence(ev_b.id, db_b)

    assert company_b is not None
    assert company_b.normalized_name == "acme technology"
    assert company_b is not company_a, "must produce two distinct Company rows"

    dup_reviews = [o for o in db_b._added if isinstance(o, DuplicateReview)]
    assert len(dup_reviews) == 0, (
        "Acme Tech / Acme Technology must NOT be flagged — "
        "name similarity is below the 0.85 threshold"
    )


# ─── Test 6: company_id is populated after resolution ────────────────────────


def test_evidence_company_id_is_populated_after_resolution():
    """evidence_items.company_id is non-None after successful resolution."""
    ev = _make_evidence(uei="UEI_TEST_001")
    db = _make_session(evidence=ev)

    assert ev.company_id is None, "company_id must start as NULL"

    result = resolve_company_for_evidence(ev.id, db)

    assert result is not None
    assert ev.company_id is not None, "company_id must be set after resolution"
    assert ev.company_id == result.id


# ─── Test 7: missing company_name → returns None, no crash ───────────────────


def test_missing_company_name_returns_none_without_crash():
    """Missing company_name returns None without raising — evidence row is skipped."""
    ev = _make_evidence(company_name=None)
    db = _make_session(evidence=ev)

    result = resolve_company_for_evidence(ev.id, db)

    assert result is None
    assert len(db._added) == 0, "must not add any DB rows when company_name is missing"


# ─── Tests 8–11: NAICS mapping ────────────────────────────────────────────────


def test_new_company_gets_naics_code_from_evidence():
    """New company created from evidence receives naics_code and naics_description."""
    ev = _make_evidence(
        naics_code="541511",
        naics_description="Custom Computer Programming Services",
    )
    db = _make_session(evidence=ev)

    result = resolve_company_for_evidence(ev.id, db)

    assert result is not None
    companies = [o for o in db._added if isinstance(o, Company)]
    assert len(companies) == 1
    assert companies[0].naics_code == "541511"
    assert companies[0].naics_description == "Custom Computer Programming Services"


def test_existing_company_null_naics_gets_filled_from_evidence():
    """Existing company with NULL naics_code is updated when evidence provides one."""
    existing_company = Company(
        id=uuid.uuid4(),
        canonical_name="Acme Federal Services",
        normalized_name="acme federal services",
        state="VA",
        naics_code=None,
    )
    identifier_mock = MagicMock()
    identifier_mock.company_id = existing_company.id

    ev = _make_evidence(uei="UEI123456789", naics_code="541511")
    db = _make_session(evidence=ev, identifier=identifier_mock, company=existing_company)

    result = resolve_company_for_evidence(ev.id, db)

    assert result is existing_company
    assert existing_company.naics_code == "541511", (
        "null naics_code must be filled when evidence provides one"
    )


def test_existing_company_non_null_naics_not_overwritten():
    """Existing company with a non-null naics_code is never overwritten by new evidence."""
    existing_company = Company(
        id=uuid.uuid4(),
        canonical_name="Acme Federal Services",
        normalized_name="acme federal services",
        state="VA",
        naics_code="336411",  # already set
    )
    identifier_mock = MagicMock()
    identifier_mock.company_id = existing_company.id

    ev = _make_evidence(uei="UEI123456789", naics_code="541511")  # different NAICS
    db = _make_session(evidence=ev, identifier=identifier_mock, company=existing_company)

    resolve_company_for_evidence(ev.id, db)

    assert existing_company.naics_code == "336411", (
        "existing non-null naics_code must never be overwritten"
    )


def test_evidence_without_naics_resolves_normally():
    """Evidence with no naics_code still resolves to a Company without crashing."""
    ev = _make_evidence()  # naics_code=None by default
    db = _make_session(evidence=ev)

    result = resolve_company_for_evidence(ev.id, db)

    assert result is not None
    companies = [o for o in db._added if isinstance(o, Company)]
    assert len(companies) == 1
    assert companies[0].naics_code is None, "naics_code must be None when evidence has none"
