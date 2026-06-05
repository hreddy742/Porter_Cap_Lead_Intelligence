"""
Read-only health reporting for completed pipeline runs.

These functions never write, never call external APIs, and never launch the pipeline.
They read pipeline_runs, source_runs, and source_registry and return plain dicts.
"""

from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models import PipelineRun, SourceRegistry, SourceRun


def get_latest_pipeline_health(db: Session) -> dict:
    """
    Summarise the most recent pipeline run.

    Returns has_runs=False with all-None/zero values if no run exists yet.
    duration_seconds is only populated when both started_at and ended_at are present.
    evidence_items_created / companies_resolved / signals_created / companies_scored
    are returned as None — those counts are not captured in pipeline_runs or source_runs.
    """
    run: PipelineRun | None = (
        db.query(PipelineRun)
        .order_by(PipelineRun.started_at.desc())
        .first()
    )

    if run is None:
        return {
            "has_runs": False,
            "pipeline_run_id": None,
            "status": None,
            "started_at": None,
            "finished_at": None,
            "duration_seconds": None,
            "sources_total": 0,
            "sources_succeeded": 0,
            "sources_failed": 0,
            "raw_events_processed": None,
            "evidence_items_created": None,
            "companies_resolved": None,
            "signals_created": None,
            "companies_scored": None,
            "errors": [],
        }

    source_runs: list[SourceRun] = (
        db.query(SourceRun)
        .filter(SourceRun.pipeline_run_id == run.id)
        .all()
    )

    ended_at = run.ended_at
    duration_seconds: float | None = None
    if run.started_at and ended_at:
        duration_seconds = (ended_at - run.started_at).total_seconds()

    sources_total = len(source_runs)
    sources_succeeded = sum(1 for sr in source_runs if sr.status == "completed")
    sources_failed = sum(1 for sr in source_runs if sr.status == "failed")
    raw_events_processed = sum((sr.records_fetched or 0) for sr in source_runs)

    errors = [
        {
            "source_run_id": str(sr.id),
            "source_id": str(sr.source_id),
            "error_text": sr.error_text,
            "started_at": sr.started_at,
            "finished_at": sr.finished_at,
        }
        for sr in source_runs
        if sr.status == "failed"
    ]

    return {
        "has_runs": True,
        "pipeline_run_id": run.id,
        "status": run.status,
        "started_at": run.started_at,
        "finished_at": ended_at,
        "duration_seconds": duration_seconds,
        "sources_total": sources_total,
        "sources_succeeded": sources_succeeded,
        "sources_failed": sources_failed,
        "raw_events_processed": raw_events_processed,
        "evidence_items_created": None,
        "companies_resolved": None,
        "signals_created": None,
        "companies_scored": None,
        "errors": errors,
    }


def get_source_health(db: Session) -> list[dict]:
    """
    Return one health dict per source in source_registry, ordered by name.

    last_success_at / last_failure_at come directly from the source_registry row.
    last_error is taken from the most recent failed source_run (within the 5 most recent runs).
    recent_runs contains the 5 most recent source_runs for that source, newest first.
    """
    sources: list[SourceRegistry] = (
        db.query(SourceRegistry)
        .order_by(SourceRegistry.name)
        .all()
    )

    result = []
    for source in sources:
        recent_runs: list[SourceRun] = (
            db.query(SourceRun)
            .filter(SourceRun.source_id == source.id)
            .order_by(SourceRun.started_at.desc())
            .limit(5)
            .all()
        )

        last_error: str | None = None
        for sr in recent_runs:
            if sr.status == "failed" and sr.error_text:
                last_error = sr.error_text
                break

        result.append({
            "source_id": source.id,
            "name": source.name,
            "enabled": source.enabled,
            "status": source.status,
            "last_success_at": source.last_success_at,
            "last_failure_at": source.last_failure_at,
            "last_error": last_error,
            "recent_runs": [
                {
                    "source_run_id": str(sr.id),
                    "pipeline_run_id": str(sr.pipeline_run_id),
                    "started_at": sr.started_at,
                    "finished_at": sr.finished_at,
                    "status": sr.status,
                    "records_fetched": getattr(sr, "records_fetched", None),
                    "records_valid": getattr(sr, "records_valid", None),
                    "error_text": sr.error_text,
                }
                for sr in recent_runs
            ],
        })

    return result


def get_pipeline_run_summary(pipeline_run_id: UUID, db: Session) -> dict | None:
    """
    Return a detailed summary of a specific pipeline run, or None if not found.

    Includes per-source-run breakdowns and an errors list for any failed source_runs.
    Count fields that are not tracked in the schema (evidence, signals, companies) are
    returned as None rather than invented.
    """
    run: PipelineRun | None = (
        db.query(PipelineRun)
        .filter(PipelineRun.id == pipeline_run_id)
        .first()
    )

    if run is None:
        return None

    source_runs: list[SourceRun] = (
        db.query(SourceRun)
        .filter(SourceRun.pipeline_run_id == run.id)
        .all()
    )

    ended_at = run.ended_at
    duration_seconds: float | None = None
    if run.started_at and ended_at:
        duration_seconds = (ended_at - run.started_at).total_seconds()

    errors = [
        {
            "source_run_id": str(sr.id),
            "source_id": str(sr.source_id),
            "error_text": sr.error_text,
            "started_at": sr.started_at,
            "finished_at": sr.finished_at,
        }
        for sr in source_runs
        if sr.status == "failed"
    ]

    return {
        "pipeline_run_id": run.id,
        "status": run.status,
        "trigger": getattr(run, "trigger", None),
        "started_at": run.started_at,
        "finished_at": ended_at,
        "duration_seconds": duration_seconds,
        "total_records": getattr(run, "total_records", None),
        "total_hot": getattr(run, "total_hot", None),
        "total_warm": getattr(run, "total_warm", None),
        "total_cold": getattr(run, "total_cold", None),
        "total_archive": getattr(run, "total_archive", None),
        "quarantine_count": getattr(run, "quarantine_count", None),
        "error_summary": getattr(run, "error_summary", None),
        "source_runs": [
            {
                "source_run_id": str(sr.id),
                "source_id": str(sr.source_id),
                "started_at": sr.started_at,
                "finished_at": sr.finished_at,
                "status": sr.status,
                "records_fetched": getattr(sr, "records_fetched", None),
                "records_valid": getattr(sr, "records_valid", None),
                "records_skipped": getattr(sr, "records_skipped", None),
                "quarantine_count": getattr(sr, "quarantine_count", None),
                "error_text": sr.error_text,
            }
            for sr in source_runs
        ],
        "errors": errors,
    }
