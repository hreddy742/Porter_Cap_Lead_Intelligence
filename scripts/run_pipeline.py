"""
Pipeline entrypoint — runs one full pipeline cycle and exits with an appropriate code.

Usage:
    python scripts/run_pipeline.py
    docker compose run --rm app python scripts/run_pipeline.py

Exit codes:
    0  — pipeline completed (all sources succeeded, or no sources configured)
    1  — pipeline failed or partial (at least one source failed)
"""

import json
import sys

import structlog

from app.db.session import SessionLocal
from app.pipeline.orchestrator import run_pipeline

log = structlog.get_logger()


def main() -> None:
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

    if status == "completed":
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
