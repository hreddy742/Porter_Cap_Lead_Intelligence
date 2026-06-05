"""
Tests for app/processing/suppression.py.

check_suppression() and import_suppression_csv() are both tested here.
All tests use MagicMock sessions — no real database required.

Priority order under test:
  1. uei  2. domain  3. sf_lead_id  4. sf_account_id  5. name_state
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest

from app.db.models import Company, CompanyIdentifier, SuppressionList
from app.processing.suppression import check_suppression, import_suppression_csv
from app.utils.normalize import normalize_for_suppression_key


# ─── Shared builders ──────────────────────────────────────────────────────────


def _make_company(*, name: str = "Acme Federal Services LLC", state: str = "TX") -> MagicMock:
    c = MagicMock(spec=Company)
    c.id = uuid.uuid4()
    c.canonical_name = name
    c.state = state
    return c


def _make_identifier(*, company_id, id_type: str, id_value: str) -> MagicMock:
    ident = MagicMock(spec=CompanyIdentifier)
    ident.id = uuid.uuid4()
    ident.company_id = company_id
    ident.id_type = id_type
    ident.id_value = id_value
    return ident


def _make_suppression_entry(
    *,
    match_type: str,
    match_value: str,
    reason: str = "existing_customer",
) -> MagicMock:
    e = MagicMock(spec=SuppressionList)
    e.id = uuid.uuid4()
    e.match_type = match_type
    e.match_value = match_value
    e.reason = reason
    e.active = True
    return e


def _make_session(
    *,
    company: MagicMock | None = None,
    identifiers: list | None = None,
    suppression_entries: list | None = None,
) -> MagicMock:
    """
    Build a mocked SQLAlchemy Session.

    execute() is called twice by check_suppression:
      call 0 → CompanyIdentifier rows
      call 1 → SuppressionList rows
    """
    added: list = []
    call_count = [0]

    def _execute(stmt):
        result = MagicMock()
        n = call_count[0]
        call_count[0] += 1
        if n == 0:
            result.scalars.return_value.all.return_value = identifiers or []
        else:
            result.scalars.return_value.all.return_value = suppression_entries or []
        return result

    def _add(obj):
        added.append(obj)

    def _get(cls, pk):
        if cls is Company:
            return company
        return None

    s = MagicMock()
    s.get.side_effect = _get
    s.add.side_effect = _add
    s.execute.side_effect = _execute
    s._added = added
    return s


# ─── Test 1: UEI match suppresses the company ────────────────────────────────


def test_uei_match_suppresses_company():
    """A suppression entry matched by UEI causes suppressed=True."""
    company = _make_company()
    uei = "1234567890ABC"
    ident = _make_identifier(company_id=company.id, id_type="uei", id_value=uei)
    supp = _make_suppression_entry(match_type="uei", match_value=uei, reason="existing_customer")

    db = _make_session(company=company, identifiers=[ident], suppression_entries=[supp])
    result = check_suppression(company.id, db)

    assert result["suppressed"] is True
    assert result["match_type"] == "uei"
    assert result["match_value"] == uei
    assert result["reason"] == "existing_customer"
    assert result["suppression_id"] == supp.id


# ─── Test 2: Domain match suppresses the company ─────────────────────────────


def test_domain_match_suppresses_company():
    """A suppression entry matched by domain causes suppressed=True."""
    company = _make_company()
    domain = "acme.com"
    ident = _make_identifier(company_id=company.id, id_type="domain", id_value=domain)
    supp = _make_suppression_entry(match_type="domain", match_value=domain, reason="do_not_contact")

    db = _make_session(company=company, identifiers=[ident], suppression_entries=[supp])
    result = check_suppression(company.id, db)

    assert result["suppressed"] is True
    assert result["match_type"] == "domain"
    assert result["match_value"] == domain
    assert result["reason"] == "do_not_contact"


# ─── Test 3: UEI priority beats domain ───────────────────────────────────────


def test_uei_wins_over_domain_when_both_match():
    """When both UEI and domain entries exist, UEI is returned (higher priority)."""
    company = _make_company()
    uei = "PRIORITY_UEI_001"
    domain = "priority-test.com"

    ident_uei = _make_identifier(company_id=company.id, id_type="uei", id_value=uei)
    ident_domain = _make_identifier(company_id=company.id, id_type="domain", id_value=domain)

    supp_uei = _make_suppression_entry(match_type="uei", match_value=uei, reason="existing_customer")
    supp_domain = _make_suppression_entry(match_type="domain", match_value=domain, reason="do_not_contact")

    db = _make_session(
        company=company,
        identifiers=[ident_uei, ident_domain],
        suppression_entries=[supp_uei, supp_domain],
    )
    result = check_suppression(company.id, db)

    assert result["suppressed"] is True
    assert result["match_type"] == "uei", "UEI must win over domain in priority order"
    assert result["match_value"] == uei


# ─── Test 4: name_state match suppresses the company ─────────────────────────


def test_name_state_match_suppresses_company():
    """A suppression entry matched by name_state key causes suppressed=True."""
    company = _make_company(name="Acme Federal Services LLC", state="TX")
    key = normalize_for_suppression_key("Acme Federal Services LLC", "TX")
    supp = _make_suppression_entry(match_type="name_state", match_value=key)

    db = _make_session(company=company, identifiers=[], suppression_entries=[supp])
    result = check_suppression(company.id, db)

    assert result["suppressed"] is True
    assert result["match_type"] == "name_state"
    assert result["match_value"] == key


# ─── Test 5: Clean company is not suppressed ─────────────────────────────────


def test_clean_company_not_suppressed():
    """A company with no matching suppression entries returns suppressed=False."""
    company = _make_company()
    ident = _make_identifier(company_id=company.id, id_type="uei", id_value="CLEAN_CO_001")
    # Suppression list has a different UEI — no match.
    supp = _make_suppression_entry(match_type="uei", match_value="DIFFERENT_CO_999")

    db = _make_session(company=company, identifiers=[ident], suppression_entries=[supp])
    result = check_suppression(company.id, db)

    assert result["suppressed"] is False
    assert result["reason"] is None
    assert result["match_type"] is None
    assert result["match_value"] is None
    assert result["suppression_id"] is None


# ─── Test 6: Inactive suppression row does not suppress ──────────────────────


def test_inactive_suppression_does_not_suppress():
    """active=False entries are excluded by the WHERE clause and must not suppress."""
    company = _make_company()
    uei = "INACTIVE_TEST_UEI"
    ident = _make_identifier(company_id=company.id, id_type="uei", id_value=uei)
    # The real query filters WHERE active = TRUE, so the inactive entry is never returned.
    # We model this by returning an empty suppression list.
    db = _make_session(company=company, identifiers=[ident], suppression_entries=[])
    result = check_suppression(company.id, db)

    assert result["suppressed"] is False


# ─── Test 7: CSV import inserts valid rows ───────────────────────────────────


def test_csv_import_inserts_valid_rows(tmp_path):
    """import_suppression_csv inserts one SuppressionList row per valid CSV line."""
    csv_file = tmp_path / "suppression.csv"
    csv_file.write_text(
        "match_type,match_value,reason,source\n"
        "uei,ABC123DEF456,existing_customer,salesforce\n"
        "domain,acme.com,do_not_contact,csv_import\n"
        "name_state,widget co|CA,existing_customer,salesforce\n",
        encoding="utf-8",
    )

    added: list = []
    db = MagicMock()
    db.add.side_effect = lambda obj: added.append(obj)

    count = import_suppression_csv(str(csv_file), db)

    assert count == 3
    suppression_rows = [o for o in added if isinstance(o, SuppressionList)]
    assert len(suppression_rows) == 3


# ─── Test 8: CSV import skips invalid and blank rows ─────────────────────────


def test_csv_import_skips_invalid_rows(tmp_path):
    """import_suppression_csv skips blank rows and rows missing required fields."""
    csv_file = tmp_path / "suppression.csv"
    csv_file.write_text(
        "match_type,match_value,reason\n"
        "uei,VALID001,existing_customer\n"  # valid — inserted
        ",,\n"                               # all blank — skip
        ",MISSING_TYPE,existing_customer\n"  # missing match_type — skip
        "domain,,existing_customer\n"        # missing match_value — skip
        "domain,some.com,\n"                 # missing reason — skip
    )

    added: list = []
    db = MagicMock()
    db.add.side_effect = lambda obj: added.append(obj)

    count = import_suppression_csv(str(csv_file), db)

    assert count == 1
    suppression_rows = [o for o in added if isinstance(o, SuppressionList)]
    assert len(suppression_rows) == 1


# ─── Test 9: Company not found returns suppressed = False ────────────────────


def test_company_not_found_returns_not_suppressed():
    """A company_id that does not exist returns suppressed=False without crashing."""
    db = _make_session(company=None)
    result = check_suppression(uuid.uuid4(), db)

    assert result == {
        "suppressed": False,
        "reason": None,
        "route": None,
        "match_type": None,
        "match_value": None,
        "suppression_id": None,
    }


# ─── Test 10: Result always contains all required keys ───────────────────────


def test_suppression_result_includes_all_required_fields():
    """check_suppression always returns a dict with all five required keys."""
    company = _make_company()
    uei = "FULLRESULT_UEI_42"
    supp_id = uuid.uuid4()

    ident = _make_identifier(company_id=company.id, id_type="uei", id_value=uei)
    supp = _make_suppression_entry(match_type="uei", match_value=uei, reason="existing_customer")
    supp.id = supp_id

    db = _make_session(company=company, identifiers=[ident], suppression_entries=[supp])
    result = check_suppression(company.id, db)

    for key in ("suppressed", "reason", "route", "match_type", "match_value", "suppression_id"):
        assert key in result, f"result missing key: {key!r}"

    assert result["suppressed"] is True
    assert result["reason"] == "existing_customer"
    assert result["match_type"] == "uei"
    assert result["match_value"] == uei
    assert result["suppression_id"] == supp_id


# ═══════════════════════════════════════════════════════════════════════════════
# Route classification tests (Task 05 business-logic correction)
# ═══════════════════════════════════════════════════════════════════════════════


def _suppressed_result(*, reason: str, match_type: str = "uei") -> dict:
    """Build a minimal suppressed company + session and return check_suppression result."""
    company = _make_company()
    value = "ROUTE_TEST_VALUE"
    ident = _make_identifier(company_id=company.id, id_type=match_type, id_value=value)
    supp = _make_suppression_entry(match_type=match_type, match_value=value, reason=reason)
    db = _make_session(company=company, identifiers=[ident], suppression_entries=[supp])
    return check_suppression(company.id, db)


# ─── Route test 1: existing_customer → account_review ────────────────────────


def test_route_existing_customer_is_account_review():
    """existing_customer reason routes to account_review, not hard_block."""
    result = _suppressed_result(reason="existing_customer")
    assert result["suppressed"] is True
    assert result["route"] == "account_review"


# ─── Route test 2: do_not_contact → hard_block ───────────────────────────────


def test_route_do_not_contact_is_hard_block():
    """do_not_contact reason routes to hard_block."""
    result = _suppressed_result(reason="do_not_contact")
    assert result["suppressed"] is True
    assert result["route"] == "hard_block"


# ─── Route test 3: compliance_blocked → hard_block ───────────────────────────


def test_route_compliance_blocked_is_hard_block():
    """compliance_blocked reason routes to hard_block."""
    result = _suppressed_result(reason="compliance_blocked")
    assert result["suppressed"] is True
    assert result["route"] == "hard_block"


# ─── Route test 4: sf_account_id match → account_review ─────────────────────


def test_route_sf_account_id_match_is_account_review():
    """A match on sf_account_id match_type routes to account_review regardless of reason."""
    company = _make_company()
    sf_id = "0015000001SFACC"
    ident = _make_identifier(company_id=company.id, id_type="sf_account_id", id_value=sf_id)
    supp = _make_suppression_entry(
        match_type="sf_account_id", match_value=sf_id, reason="other_reason"
    )
    db = _make_session(company=company, identifiers=[ident], suppression_entries=[supp])
    result = check_suppression(company.id, db)

    assert result["suppressed"] is True
    assert result["match_type"] == "sf_account_id"
    assert result["route"] == "account_review"


# ─── Route test 5: sf_lead_id match → existing_lead_review ──────────────────


def test_route_sf_lead_id_match_is_existing_lead_review():
    """A match on sf_lead_id match_type routes to existing_lead_review."""
    company = _make_company()
    sf_id = "00Q5000001SFLEAD"
    ident = _make_identifier(company_id=company.id, id_type="sf_lead_id", id_value=sf_id)
    supp = _make_suppression_entry(
        match_type="sf_lead_id", match_value=sf_id, reason="other_reason"
    )
    db = _make_session(company=company, identifiers=[ident], suppression_entries=[supp])
    result = check_suppression(company.id, db)

    assert result["suppressed"] is True
    assert result["match_type"] == "sf_lead_id"
    assert result["route"] == "existing_lead_review"


# ─── Route test 6: duplicate reason → duplicate_review ───────────────────────


def test_route_duplicate_reason_is_duplicate_review():
    """duplicate reason routes to duplicate_review."""
    result = _suppressed_result(reason="duplicate")
    assert result["suppressed"] is True
    assert result["route"] == "duplicate_review"


# ─── Route test 7: unknown reason → research_review ─────────────────────────


def test_route_unknown_reason_is_research_review():
    """An unrecognised suppression reason routes to research_review."""
    result = _suppressed_result(reason="some_internal_flag")
    assert result["suppressed"] is True
    assert result["route"] == "research_review"


# ─── Route test 8: clean company → suppressed=False, route=None ─────────────


def test_route_clean_company_has_no_route():
    """A company with no suppression match returns suppressed=False and route=None."""
    company = _make_company()
    ident = _make_identifier(company_id=company.id, id_type="uei", id_value="CLEAN_ROUTE_001")
    supp = _make_suppression_entry(match_type="uei", match_value="DIFFERENT_ROUTE_999")
    db = _make_session(company=company, identifiers=[ident], suppression_entries=[supp])
    result = check_suppression(company.id, db)

    assert result["suppressed"] is False
    assert result["route"] is None
