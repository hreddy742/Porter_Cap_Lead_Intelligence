"""
Pipeline cron entrypoint.
Run this on a schedule (e.g. nightly at 2am):
    docker compose run --rm app python -m scripts.run_pipeline
"""

import sys

import structlog

log = structlog.get_logger()


def main() -> None:
    log.info("pipeline_entrypoint_starting")
    # TODO: import and call run_pipeline() once orchestrator is built (Week 2)
    # from app.pipeline.orchestrator import run_pipeline
    # result = run_pipeline(trigger="cron")
    # log.info("pipeline_complete", status=result.status)
    log.info("pipeline_entrypoint_placeholder — build orchestrator in Week 2")
    sys.exit(0)


if __name__ == "__main__":
    main()
