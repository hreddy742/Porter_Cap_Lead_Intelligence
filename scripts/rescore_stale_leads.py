"""
Rescore stale leads whose tier is 'warm' but whose latest score is 70-74.

These companies were scored when _TIER_HOT was 75, which made 70-74 warm.
The threshold was lowered to 70 on 2026-06-22. This script inserts a NEW
versioned lead_scores row with tier='hot' and updates lead_candidates.tier.

The underlying total_score is unchanged — only the tier interpretation changed.
Historical lead_scores rows are never touched (founding rule: scores are versioned).

score_company() is intentionally NOT called here. That function runs mandatory
gates, and the duplicate_active gate correctly blocks re-scoring of companies
that already have an active lead_candidate. This script is a targeted data
correction, not a pipeline re-run.

Usage:
    python scripts/rescore_stale_leads.py
"""
import os
import sys
import uuid as uuid_module

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import structlog
from sqlalchemy import func, and_, select

from app.db.session import SessionLocal
from app.db.models import LeadCandidate, Company, LeadScore, ScoringConfig
from app.processing.scoring import _assign_tier

log = structlog.get_logger()


def _find_stale_candidates(db):
    """Return (lead_candidate, company, latest_score) for warm leads scoring 70-74."""
    latest_subq = (
        select(
            LeadScore.lead_candidate_id,
            func.max(LeadScore.computed_at).label("latest_at"),
        )
        .group_by(LeadScore.lead_candidate_id)
        .subquery()
    )

    rows = db.execute(
        select(LeadCandidate, Company, LeadScore)
        .join(Company, LeadCandidate.company_id == Company.id)
        .join(
            latest_subq,
            LeadCandidate.id == latest_subq.c.lead_candidate_id,
        )
        .join(
            LeadScore,
            and_(
                LeadScore.lead_candidate_id == LeadCandidate.id,
                LeadScore.computed_at == latest_subq.c.latest_at,
            ),
        )
        .where(LeadCandidate.tier == "warm")
        .where(LeadScore.total_score.between(70, 74))
        .where(LeadCandidate.deleted_at.is_(None))
        .where(LeadCandidate.status == "active")
    ).all()

    return rows


def main() -> None:
    db = SessionLocal()
    updated = []
    errors = []

    try:
        stale = _find_stale_candidates(db)
        print(f"Found {len(stale)} stale warm leads in the 70-74 range.")

        # Load active scoring config once — new lead_scores rows reference it.
        active_cfg = db.execute(
            select(ScoringConfig).where(ScoringConfig.active == True)  # noqa: E712
        ).scalars().first()

        if active_cfg is None:
            print("ERROR: no active scoring_config found. Aborting.")
            return

        print(f"Using scoring config: {active_cfg.version_label} (id={active_cfg.id})")
        print()

        for candidate, company, old_score in stale:
            old_tier = candidate.tier
            old_total = old_score.total_score
            new_tier = _assign_tier(old_total)

            if new_tier == old_tier:
                # Should not happen for 70-74 range, but guard anyway.
                print(f"  SKIP {company.canonical_name}: score={old_total} still maps to '{new_tier}'")
                continue

            try:
                # Insert a NEW versioned lead_scores row with the corrected tier.
                # component_breakdown and evidence_ids are copied from the old row —
                # the underlying scoring math is unchanged; only the tier threshold changed.
                new_score_row = LeadScore(
                    id=uuid_module.uuid4(),
                    lead_candidate_id=candidate.id,
                    scoring_config_id=active_cfg.id,
                    config_hash=active_cfg.config_hash,
                    total_score=old_total,
                    tier=new_tier,
                    component_breakdown=old_score.component_breakdown,
                    evidence_ids=old_score.evidence_ids,
                    gate_result="passed",
                    gate_reasons=[],
                )
                db.add(new_score_row)

                # Update the live lead_candidate tier to reflect the correction.
                candidate.tier = new_tier
                candidate.current_score = old_total

                db.commit()

                log.info(
                    "rescore_stale_lead_updated",
                    company_name=company.canonical_name,
                    old_tier=old_tier,
                    new_tier=new_tier,
                    old_score=old_total,
                    new_score=old_total,
                )
                updated.append(
                    {
                        "name": company.canonical_name,
                        "old_tier": old_tier,
                        "new_tier": new_tier,
                        "score": old_total,
                    }
                )
            except Exception as exc:
                db.rollback()
                log.error(
                    "rescore_stale_lead_failed",
                    company_name=company.canonical_name,
                    error=str(exc),
                )
                errors.append({"name": company.canonical_name, "error": str(exc)})
    finally:
        db.close()

    print("=" * 62)
    print("  Rescore summary")
    print("=" * 62)
    print(f"  Processed : {len(stale)}")
    print(f"  Updated   : {len(updated)}")
    print(f"  Errors    : {len(errors)}")
    print()

    warm_to_hot = [r for r in updated if r["old_tier"] == "warm" and r["new_tier"] == "hot"]
    if warm_to_hot:
        print(f"  warm -> hot ({len(warm_to_hot)}):")
        for r in warm_to_hot:
            print(f"    {r['name']}  score:{r['score']}")
    else:
        print("  No leads moved from warm to hot.")

    if errors:
        print()
        print(f"  Errors ({len(errors)}):")
        for e in errors:
            print(f"    {e['name']}: {e['error']}")


if __name__ == "__main__":
    main()
