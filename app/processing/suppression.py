"""
Suppression engine — Phase 1.

check_suppression: check whether a company is on the suppression list.
import_suppression_csv: bulk-load suppression entries from a CSV file.

Priority order for matching (first match wins):
  1. uei
  2. domain
  3. sf_lead_id
  4. sf_account_id
  5. name_state
"""

from __future__ import annotations

import csv
from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Company, CompanyIdentifier, SuppressionList
from app.utils.normalize import normalize_for_suppression_key

_PRIORITY_ORDER = ["uei", "domain", "sf_lead_id", "sf_account_id", "name_state"]

_NOT_SUPPRESSED: dict = {
    "suppressed": False,
    "reason": None,
    "route": None,
    "match_type": None,
    "match_value": None,
    "suppression_id": None,
}


def _classify_route(reason: str, match_type: str) -> str:
    """
    Map a suppression match to a downstream routing destination.

    Rules (first match wins):
      do_not_contact / compliance_blocked  → hard_block          (legal/compliance; never queue)
      existing_customer                    → account_review       (re-engagement, not new lead)
      sf_account_id match                  → account_review       (known Salesforce account)
      sf_lead_id match                     → existing_lead_review (already in Salesforce as lead)
      duplicate                            → duplicate_review     (data-quality resolution)
      anything else                        → research_review      (unknown; needs human triage)
    """
    if reason in ("do_not_contact", "compliance_blocked"):
        return "hard_block"
    if reason == "existing_customer":
        return "account_review"
    if match_type == "sf_account_id":
        return "account_review"
    if match_type == "sf_lead_id":
        return "existing_lead_review"
    if reason == "duplicate":
        return "duplicate_review"
    return "research_review"


def check_suppression(company_id: UUID, db: Session) -> dict:
    """
    Check whether company_id is on the active suppression list.

    Returns a dict with keys:
        suppressed     bool
        reason         str | None
        route          str | None   ("hard_block", "account_review", "existing_lead_review",
                                     "duplicate_review", "research_review", or None)
        match_type     str | None
        match_value    str | None
        suppression_id UUID | None

    First match in priority order wins. Does not write to the database.
    """
    company: Company | None = db.get(Company, company_id)
    if company is None:
        return dict(_NOT_SUPPRESSED)

    # Load all identifiers for this company.
    ident_rows = (
        db.execute(
            select(CompanyIdentifier).where(CompanyIdentifier.company_id == company_id)
        )
        .scalars()
        .all()
    )
    id_map: dict[str, str] = {row.id_type: row.id_value for row in ident_rows}

    # Build the name_state key (last-priority fallback).
    name_state_key: str | None = None
    if company.canonical_name and company.state:
        try:
            name_state_key = normalize_for_suppression_key(company.canonical_name, company.state)
        except ValueError:
            pass

    # Load all active suppression entries in one query.
    supp_rows = (
        db.execute(
            select(SuppressionList).where(SuppressionList.active == True)  # noqa: E712
        )
        .scalars()
        .all()
    )

    # Index suppression entries by (match_type, match_value) for O(1) lookup.
    supp_index: dict[tuple[str, str], SuppressionList] = {
        (row.match_type, row.match_value): row for row in supp_rows
    }

    for match_type in _PRIORITY_ORDER:
        if match_type == "name_state":
            candidate_value = name_state_key
        else:
            candidate_value = id_map.get(match_type)

        if not candidate_value:
            continue

        entry = supp_index.get((match_type, candidate_value))
        if entry is not None:
            return {
                "suppressed": True,
                "reason": entry.reason,
                "route": _classify_route(entry.reason, match_type),
                "match_type": match_type,
                "match_value": candidate_value,
                "suppression_id": entry.id,
            }

    return dict(_NOT_SUPPRESSED)


def import_suppression_csv(csv_path: str, db: Session) -> int:
    """
    Import suppression entries from a CSV file.

    Required columns: match_type, match_value, reason
    Optional columns: source  (defaults to "csv_import")

    Skips blank rows and rows missing required fields.
    Skips individual bad rows without crashing.
    Returns the count of rows successfully inserted.
    """
    today = date.today()
    inserted = 0

    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                match_type = (row.get("match_type") or "").strip()
                match_value = (row.get("match_value") or "").strip()
                reason = (row.get("reason") or "").strip()

                if not match_type or not match_value or not reason:
                    continue

                source = (row.get("source") or "").strip() or "csv_import"

                entry = SuppressionList(
                    match_type=match_type,
                    match_value=match_value,
                    reason=reason,
                    source=source,
                    active=True,
                    import_date=today,
                )
                db.add(entry)
                inserted += 1
            except Exception:
                continue

    return inserted
