"""
One-off cleanup: archive existing lead_candidates that are social-service
nonprofits per Gate 13 (app/processing/gates.py::gate_13_social_service_nonprofit).

UNITED COMMUNITY ACTION PROGRAM (NAICS 624410) and JERSEY BATTERED WOMENS
SERVICE (NAICS 624221) were scored before Gate 13 existed. This script
directly mutates their lead_candidates.tier/sector_excluded, the same
pattern as scripts/rescore_stale_leads.py — a targeted data correction,
not a pipeline re-run. score_company() is intentionally NOT called; the
duplicate_active gate would block re-scoring an already-active candidate.

Usage:
    python scripts/archive_social_service_leads.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import structlog
from sqlalchemy import select

from app.db.session import SessionLocal
from app.db.models import Company, LeadCandidate
from app.processing.gates import gate_13_social_service_nonprofit

log = structlog.get_logger()


def main() -> None:
    db = SessionLocal()
    archived = []
    errors = []

    try:
        rows = (
            db.execute(
                select(LeadCandidate, Company)
                .join(Company, LeadCandidate.company_id == Company.id)
                .where(LeadCandidate.deleted_at.is_(None))
                .where(LeadCandidate.status == "active")
            )
            .all()
        )
        print(f"Checking {len(rows)} active lead candidates against Gate 13.")

        for candidate, company in rows:
            result = gate_13_social_service_nonprofit(
                company.canonical_name, company.naics_code
            )
            if result.passed:
                continue

            try:
                old_tier = candidate.tier
                candidate.tier = "archive"
                candidate.sector_excluded = True
                candidate.sector_excluded_reason = result.reason
                db.commit()

                log.info(
                    "gate_13_lead_archived",
                    company_name=company.canonical_name,
                    old_tier=old_tier,
                    reason=result.reason,
                )
                archived.append({"name": company.canonical_name, "old_tier": old_tier})
            except Exception as exc:
                db.rollback()
                log.error(
                    "gate_13_lead_archive_failed",
                    company_name=company.canonical_name,
                    error=str(exc),
                )
                errors.append({"name": company.canonical_name, "error": str(exc)})
    finally:
        db.close()

    print("=" * 62)
    print("  Gate 13 social-service nonprofit cleanup summary")
    print("=" * 62)
    print(f"  Archived : {len(archived)}")
    print(f"  Errors   : {len(errors)}")
    print()

    if archived:
        for r in archived:
            print(f"    {r['name']}  old_tier:{r['old_tier']} -> archive")

    if errors:
        print()
        print(f"  Errors ({len(errors)}):")
        for e in errors:
            print(f"    {e['name']}: {e['error']}")


if __name__ == "__main__":
    main()
