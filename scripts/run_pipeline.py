"""
Pipeline entrypoint — supports one-shot and scheduled loop modes.

Usage:
    python scripts/run_pipeline.py
    docker compose run --rm app python scripts/run_pipeline.py

Exit codes (one-shot / PIPELINE_INTERVAL_HOURS=0 mode only):
    0  — pipeline completed (all sources succeeded, or no sources configured)
    1  — pipeline failed or partial (at least one source failed)

Environment:
    PIPELINE_INTERVAL_HOURS  Hours between pipeline runs (default: 6).
                             Set to 0 to run once and exit.
"""

import json
import os
import sys
import time

import structlog

from app.db.session import SessionLocal
from app.ops.sentry import init_sentry
from app.pipeline.orchestrator import run_pipeline

log = structlog.get_logger()


def _pipeline_cycle() -> str:
    """Run one pipeline cycle and return the status string."""
    init_sentry("pipeline")
    log.info("pipeline_entrypoint_starting")

    db = SessionLocal()
    try:
        summary = run_pipeline(db)
    finally:
        db.close()

    # Pretty-print the summary for operators reading logs/stdout
    printable = {
        k: str(v) if hasattr(v, "hex") else v  # UUID → str
        for k, v in summary.items()
    }
    print(json.dumps(printable, indent=2))

    status = summary.get("status", "failed")
    log.info("pipeline_entrypoint_finished", status=status)
    return status


def run_once() -> None:
    """Run one pipeline cycle. Raises RuntimeError if status is not 'completed'."""
    status = _pipeline_cycle()
    if status != "completed":
        raise RuntimeError(f"pipeline finished with status: {status}")


def run_loop(interval_hours: float) -> None:
    log.info("pipeline_loop_started", interval_hours=interval_hours)
    while True:
        try:
            run_once()
            log.info("pipeline_cycle_complete",
                     next_run_in_hours=interval_hours)
        except KeyboardInterrupt:
            log.info("pipeline_shutdown_requested")
            raise
        except Exception as e:
            log.error("pipeline_cycle_failed",
                      error=str(e),
                      next_retry_in_hours=interval_hours)
        time.sleep(interval_hours * 3600)


def main() -> None:
    status = _pipeline_cycle()
    if status == "completed":
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    hours = float(os.getenv("PIPELINE_INTERVAL_HOURS", "6"))
    if hours == 0:
        run_once()
    else:
        run_loop(hours)
