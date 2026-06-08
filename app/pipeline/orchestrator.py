"""
Pipeline orchestrator — Task 08.

run_pipeline(db) executes one full manual pipeline run:
  1. Create a pipeline_runs row with status="running"
  2. Load enabled sources from source_registry
  3. For each source: create source_runs, call connector, process raw events
  4. Update pipeline_runs status and return a summary dict

Each source's failure is isolated — one bad source does not stop the rest.
No Prefect, no cron, no Streamlit, no Salesforce, no AI.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import PipelineRun, RawSourceEvent, SourceRegistry, SourceRun
from app.pipeline.connectors.usaspending import USASpendingConnector
from app.processing.evidence import extract_evidence
from app.processing.resolution import resolve_company_for_evidence
from app.processing.scoring import score_company
from app.processing.signals import detect_signals_for_evidence

logger = structlog.get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _load_enabled_sources(db: Session) -> list[SourceRegistry]:
    return (
        db.execute(
            select(SourceRegistry).where(
                SourceRegistry.enabled == True,  # noqa: E712
                SourceRegistry.status == "enabled",
            )
        )
        .scalars()
        .all()
    )


def _get_raw_events(source_run_id: uuid.UUID, db: Session) -> list[RawSourceEvent]:
    return (
        db.execute(
            select(RawSourceEvent).where(
                RawSourceEvent.source_run_id == source_run_id
            )
        )
        .scalars()
        .all()
    )


def _is_usaspending(source: SourceRegistry) -> bool:
    return source.name.lower() == "usaspending"


def _execute_source(
    source: SourceRegistry,
    source_run: SourceRun,
    db: Session,
    summary: dict,
) -> None:
    """
    Run one source end-to-end: connector → evidence → resolution → signals → scoring.
    Updates summary in-place. Never raises — failures are recorded in summary.
    """
    log = logger.bind(source=source.name, source_run_id=str(source_run.id))

    try:
        # ── Step 1: dispatch connector ────────────────────────────────────────
        if _is_usaspending(source):
            connector = USASpendingConnector(db, source_run, source)
            connector.run()  # sets source_run.status; calls db.commit() internally
        else:
            log.warning("orchestrator_unknown_source_skipped", source_name=source.name)
            source_run.status = "completed"
            source_run.finished_at = _utcnow()
            db.commit()

        # ── Step 2: check whether the connector itself failed ─────────────────
        if source_run.status == "failed":
            summary["sources_failed"] += 1
            summary["errors"].append({
                "source": source.name,
                "error": source_run.error_text or "connector failed",
            })
            log.warning("orchestrator_connector_failed", source_name=source.name)
            return

        # ── Step 3: process each raw event through the pipeline ───────────────
        raw_events = _get_raw_events(source_run.id, db)
        log.info("orchestrator_events_loaded", count=len(raw_events))

        for raw_event in raw_events:
            summary["raw_events_processed"] += 1

            evidence_items = extract_evidence(raw_event.id, db)
            summary["evidence_items_created"] += len(evidence_items)

            for evidence in evidence_items:
                company = resolve_company_for_evidence(evidence.id, db)
                if company is None:
                    continue

                summary["companies_resolved"] += 1
                signals = detect_signals_for_evidence(evidence.id, db)
                summary["signals_created"] += len(signals)
                score_result = score_company(company.id, db)
                if score_result["scored"]:
                    summary["companies_scored"] += 1
                    tier = score_result.get("tier")
                    if tier == "hot":
                        summary["total_hot"] += 1
                    elif tier == "warm":
                        summary["total_warm"] += 1
                    elif tier == "cold":
                        summary["total_cold"] += 1
                    elif tier == "archive":
                        summary["total_archive"] += 1

        # ── Step 4: mark source completed ─────────────────────────────────────
        source_run.status = "completed"
        source_run.finished_at = _utcnow()
        db.commit()
        summary["sources_succeeded"] += 1
        log.info("orchestrator_source_succeeded", source_name=source.name)

    except Exception as exc:
        db.rollback()
        error_msg = str(exc)
        source_name = source.name
        summary["sources_failed"] += 1
        summary["errors"].append({"source": source_name, "error": error_msg})
        log.error("orchestrator_source_failed", source_name=source_name, error=error_msg)
        try:
            source_run.status = "failed"
            source_run.error_text = error_msg
            source_run.finished_at = _utcnow()
            db.commit()
        except Exception:
            db.rollback()


def run_pipeline(db: Session) -> dict:
    """
    Execute one full manual pipeline run using currently enabled sources.

    Returns a summary dict:
        pipeline_run_id      UUID | None
        status               "completed" | "partial" | "failed"
        sources_total        int
        sources_succeeded    int
        sources_failed       int
        raw_events_processed int
        evidence_items_created int
        companies_resolved   int
        signals_created      int
        companies_scored     int
        errors               list[dict]

    Never raises — all source failures are isolated and recorded in errors.
    """
    # Reset any stale running pipeline (e.g. from a crashed previous run)
    stale = db.execute(
        select(PipelineRun).where(PipelineRun.status == "running")
    ).scalar_one_or_none()
    if stale is not None:
        stale.status = "failed"
        stale.ended_at = _utcnow()
        stale.error_summary = "reset by subsequent run"
        db.flush()
        db.commit()
        logger.warning("orchestrator_stale_run_reset", stale_run_id=str(stale.id))

    pipeline_run = PipelineRun(
        id=uuid.uuid4(),
        status="running",
        trigger="manual",
    )
    db.add(pipeline_run)
    db.flush()
    db.commit()

    log = logger.bind(pipeline_run_id=str(pipeline_run.id))
    log.info("orchestrator_pipeline_started")

    sources = _load_enabled_sources(db)

    summary: dict = {
        "pipeline_run_id": pipeline_run.id,
        "status": "running",
        "sources_total": len(sources),
        "sources_succeeded": 0,
        "sources_failed": 0,
        "raw_events_processed": 0,
        "evidence_items_created": 0,
        "companies_resolved": 0,
        "signals_created": 0,
        "companies_scored": 0,
        "total_hot": 0,
        "total_warm": 0,
        "total_cold": 0,
        "total_archive": 0,
        "quarantine_count": 0,
        "errors": [],
    }

    for source in sources:
        source_run = SourceRun(
            id=uuid.uuid4(),
            pipeline_run_id=pipeline_run.id,
            source_id=source.id,
            status="running",
        )
        db.add(source_run)
        db.flush()
        db.commit()

        _execute_source(source, source_run, db, summary)
        summary["quarantine_count"] += source_run.quarantine_count or 0

    # ── Determine final pipeline status ───────────────────────────────────────
    if summary["sources_total"] == 0 or summary["sources_failed"] == 0:
        final_status = "completed"
    elif summary["sources_succeeded"] == 0:
        final_status = "failed"
    else:
        final_status = "partial"

    pipeline_run.status = final_status
    pipeline_run.ended_at = _utcnow()
    pipeline_run.total_records = summary["raw_events_processed"]
    pipeline_run.total_hot = summary["total_hot"]
    pipeline_run.total_warm = summary["total_warm"]
    pipeline_run.total_cold = summary["total_cold"]
    pipeline_run.total_archive = summary["total_archive"]
    pipeline_run.quarantine_count = summary["quarantine_count"]
    if summary["errors"]:
        pipeline_run.error_summary = "; ".join(e["error"] for e in summary["errors"])

    summary["status"] = final_status
    db.commit()

    log.info("orchestrator_pipeline_finished", status=final_status)
    return summary
