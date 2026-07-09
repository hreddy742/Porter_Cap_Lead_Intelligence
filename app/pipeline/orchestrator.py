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

import os
import time
import uuid
from datetime import datetime, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db.models import PipelineRun, RawSourceEvent, SourceRegistry, SourceRun
from app.ops.sentry import capture_exception
from app.pipeline.connectors.usaspending import USASpendingConnector
from app.pipeline.connectors.usaspending_subawards import USASpendingSubawardsConnector
from app.pipeline.connectors.sba_loans import SBALoansConnector
from app.pipeline.connectors.sbir_grants import SBIRGrantsConnector
from app.processing.evidence import extract_evidence
from app.processing.resolution import resolve_company_for_evidence
from app.processing.scoring import score_company
from app.processing.signals import detect_signals_for_evidence

logger = structlog.get_logger(__name__)

# Commit evidence/resolution/signal/score work every N raw events instead of
# once at the end of the whole source run. Without this, a source with 262K
# raw events already committed by its connector can run for 22+ hours in
# this loop with zero commits — an interruption anywhere in that window
# leaves every one of those raw events permanently orphaned (no evidence,
# no signals). Batching bounds orphan exposure to at most one partial batch.
BATCH_COMMIT_SIZE = 500
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_MAX_SECONDS = 60.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _load_enabled_sources(db: Session) -> list[SourceRegistry]:
    sources = list(
        db.execute(
            select(SourceRegistry).where(
                SourceRegistry.enabled == True,  # noqa: E712
                SourceRegistry.status == "enabled",
            )
        )
        .scalars()
        .all()
    )

    # SBA connector is controlled by env var in addition to the DB flag.
    # When SBA_LOANS_ENABLED=true the connector runs even if the DB row
    # has enabled=False (it is seeded disabled until UAT is complete).
    if os.getenv("SBA_LOANS_ENABLED", "false").lower() == "true":
        sba_source = db.execute(
            select(SourceRegistry).where(SourceRegistry.name == "sba_loans")
        ).scalar_one_or_none()
        if sba_source is not None and not any(s.name == "sba_loans" for s in sources):
            sources.append(sba_source)
            logger.info("sba_source_force_enabled", reason="SBA_LOANS_ENABLED=true env var")

    # SBIR connector is controlled by env var in addition to the DB flag.
    if os.getenv("SBIR_GRANTS_ENABLED", "false").lower() == "true":
        sbir_source = db.execute(
            select(SourceRegistry).where(SourceRegistry.name == "sbir_grants")
        ).scalar_one_or_none()
        if sbir_source is not None and not any(s.name == "sbir_grants" for s in sources):
            sources.append(sbir_source)
            logger.info("sbir_source_force_enabled", reason="SBIR_GRANTS_ENABLED=true env var")

    return sources


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


def _is_usaspending_subawards(source: SourceRegistry) -> bool:
    return source.name.lower() == "usaspending_subawards"


def _is_sba_loans(source: SourceRegistry) -> bool:
    return source.name.lower() == "sba_loans"


def _is_sbir_grants(source: SourceRegistry) -> bool:
    return source.name.lower() == "sbir_grants"


def _new_batch_summary() -> dict:
    return {
        "evidence_items_created": 0,
        "companies_resolved": 0,
        "signals_created": 0,
        "companies_scored": 0,
        "total_hot": 0,
        "total_warm": 0,
        "total_cold": 0,
        "total_archive": 0,
    }


def _process_raw_event(raw_event: RawSourceEvent, db: Session, batch_summary: dict) -> None:
    evidence_items = extract_evidence(raw_event.id, db)
    batch_summary["evidence_items_created"] += len(evidence_items)

    for evidence in evidence_items:
        company = resolve_company_for_evidence(evidence.id, db)
        if company is None:
            continue

        batch_summary["companies_resolved"] += 1
        signals = detect_signals_for_evidence(evidence.id, db)
        batch_summary["signals_created"] += len(signals)
        score_result = score_company(company.id, db)
        if score_result["scored"]:
            batch_summary["companies_scored"] += 1
            tier = score_result.get("tier")
            if tier == "hot":
                batch_summary["total_hot"] += 1
            elif tier == "warm":
                batch_summary["total_warm"] += 1
            elif tier == "cold":
                batch_summary["total_cold"] += 1
            elif tier == "archive":
                batch_summary["total_archive"] += 1


def _commit_batch(
    db: Session,
    log: structlog.BoundLogger,
    batch_raw_events: list[RawSourceEvent],
) -> dict:
    """
    Process and commit one batch of raw events. On a dropped connection
    (Windows TCP keepalive abort), roll back, let pool_pre_ping hand out a
    fresh connection, and replay this batch's processing so nothing is lost —
    the previously committed batches are unaffected. Mirrors the retry
    pattern in usaspending_subawards.py._commit_batch.
    """
    for attempt in range(MAX_RETRIES + 1):
        batch_summary = _new_batch_summary()
        for raw_event in batch_raw_events:
            _process_raw_event(raw_event, db, batch_summary)
        try:
            db.commit()
            return batch_summary
        except OperationalError as exc:
            log.warning(
                "orchestrator_batch_commit_db_connection_abort",
                attempt=attempt,
                batch_size=len(batch_raw_events),
                error=str(exc),
            )
            db.rollback()
            if attempt >= MAX_RETRIES:
                raise
            delay = min(BACKOFF_BASE_SECONDS * (2**attempt), BACKOFF_MAX_SECONDS)
            time.sleep(delay)
            log.info(
                "orchestrator_reprocessing_batch_after_reconnect",
                records=len(batch_raw_events),
            )

    raise AssertionError("unreachable")  # loop always returns or raises


def _merge_batch_summary(summary: dict, batch_summary: dict, batch_size: int) -> None:
    summary["raw_events_processed"] += batch_size
    for key in (
        "evidence_items_created",
        "companies_resolved",
        "signals_created",
        "companies_scored",
        "total_hot",
        "total_warm",
        "total_cold",
        "total_archive",
    ):
        summary[key] += batch_summary[key]


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
        elif _is_usaspending_subawards(source):
            connector = USASpendingSubawardsConnector(db, source_run, source)
            connector.run()
        elif _is_sba_loans(source):
            connector = SBALoansConnector(db, source_run, source)
            connector.run()
        elif _is_sbir_grants(source):
            connector = SBIRGrantsConnector(db, source_run, source)
            connector.run()
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
        # Committed in batches of BATCH_COMMIT_SIZE so a raw event's evidence,
        # signals, and score always land in the same transaction as each
        # other — never partially committed, and never orphaned relative to
        # the raw event itself (which is already committed by the connector).
        raw_events = _get_raw_events(source_run.id, db)
        log.info("orchestrator_events_loaded", count=len(raw_events))

        pending_batch: list[RawSourceEvent] = []
        for i, raw_event in enumerate(raw_events, start=1):
            pending_batch.append(raw_event)
            if i % 1000 == 0:
                log.info("orchestrator_events_progress", processed=i, total=len(raw_events))
            if len(pending_batch) >= BATCH_COMMIT_SIZE:
                batch_summary = _commit_batch(db, log, pending_batch)
                _merge_batch_summary(summary, batch_summary, len(pending_batch))
                pending_batch = []
        if pending_batch:
            batch_summary = _commit_batch(db, log, pending_batch)
            _merge_batch_summary(summary, batch_summary, len(pending_batch))

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
        sr_id = str(source_run.id) if getattr(source_run, "id", None) is not None else None
        capture_exception(exc, {"source": source_name, "source_run_id": sr_id})
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
