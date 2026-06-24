from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.schemas import DashboardSummarySchema, RunContextSchema, TierCountsSchema
from app.dashboard.review import get_latest_run_context
from app.db.session import get_session

router = APIRouter()

_WARNING = (
    "Research-ready leads only. Not sales-ready. "
    "No verified contacts. No Salesforce push. Human review required."
)


def _count_tiers(db: Session) -> dict:
    rows = db.execute(text("""
        SELECT lc.tier, COUNT(*) AS cnt
        FROM lead_candidates lc
        JOIN companies c ON c.id = lc.company_id
        WHERE lc.status = 'active'
          AND lc.deleted_at IS NULL
          AND c.deleted_at IS NULL
        GROUP BY lc.tier
    """)).fetchall()
    counts = {r.tier: int(r.cnt) for r in rows}
    pending = db.execute(text("""
        SELECT COUNT(*) FROM lead_candidates lc
        JOIN companies c ON c.id = lc.company_id
        WHERE lc.status = 'active'
          AND lc.sales_status = 'research'
          AND lc.deleted_at IS NULL
          AND c.deleted_at IS NULL
    """)).scalar() or 0
    return {
        "hot": counts.get("hot", 0),
        "warm": counts.get("warm", 0),
        "cold": counts.get("cold", 0),
        "archive": counts.get("archive", 0),
        "total": sum(counts.values()),
        "pending_review": int(pending),
    }


@router.get("/dashboard/summary", response_model=DashboardSummarySchema)
def dashboard_summary(db: Session = Depends(get_session)):
    run_ctx = get_latest_run_context(db)
    tier_counts = _count_tiers(db)

    run_context = RunContextSchema(
        has_runs=run_ctx["has_runs"],
        pipeline_run_id=(
            str(run_ctx["pipeline_run_id"]) if run_ctx["pipeline_run_id"] else None
        ),
        status=run_ctx["status"],
        started_at=str(run_ctx["started_at"]) if run_ctx["started_at"] else None,
        finished_at=str(run_ctx["finished_at"]) if run_ctx["finished_at"] else None,
        records_fetched=run_ctx["records_fetched"],
        raw_events_stored=run_ctx["raw_events_stored"],
        leads_scored=run_ctx["leads_scored"],
        errors=[
            {
                "source_run_id": e.get("source_run_id"),
                "error_text": e.get("error_text"),
            }
            for e in run_ctx["errors"]
        ],
    )

    return DashboardSummarySchema(
        run_context=run_context,
        tier_counts=TierCountsSchema(**tier_counts),
        warning=_WARNING,
    )
