"""
Read-only diagnostic report: failed source runs with unprocessed raw events.

PURPOSE
-------
After a connector failure (e.g. RemoteProtocolError on page 250), raw_source_events
rows are committed to the DB but downstream processing (evidence, company resolution,
signals, scoring) is intentionally skipped.  This report identifies those orphaned
events so operators can decide whether a recovery plan is warranted.

THIS SCRIPT IS READ-ONLY.
It does not insert, update, delete, or process any records.
It does not trigger any pipeline logic.
It does not recover or re-score any data.

Usage:
    python scripts/failed_run_events_report.py
"""
from __future__ import annotations

import sys

from sqlalchemy import text
from sqlalchemy.orm import Session

# ── SQL ────────────────────────────────────────────────────────────────────────
# Counts raw_source_events and evidence_items per failed source_run.
# COUNT(DISTINCT rse.id)           → unique raw events stored for the run
# COUNT(DISTINCT ei.raw_event_id)  → unique raw events that have ≥1 evidence row
# The DISTINCT guards against a future many-evidence-per-raw-event scenario.

_FAILED_RUNS_SQL = """
    SELECT
        sr.id                          AS source_run_id,
        s.name                         AS source_name,
        sr.status,
        sr.started_at,
        sr.finished_at,
        COALESCE(sr.records_fetched, 0) AS records_fetched,
        COALESCE(sr.records_valid,   0) AS records_valid,
        COALESCE(sr.quarantine_count,0) AS quarantine_count,
        sr.error_text,
        COUNT(DISTINCT rse.id)                  AS raw_event_count,
        COUNT(DISTINCT ei.raw_event_id)         AS events_with_evidence
    FROM source_runs sr
    JOIN source_registry s   ON s.id = sr.source_id
    LEFT JOIN raw_source_events rse ON rse.source_run_id = sr.id
    LEFT JOIN evidence_items    ei  ON ei.raw_event_id   = rse.id
    WHERE sr.status = 'failed'
    GROUP BY
        sr.id, s.name, sr.status, sr.started_at, sr.finished_at,
        sr.records_fetched, sr.records_valid, sr.quarantine_count, sr.error_text
    ORDER BY sr.started_at DESC
"""


def build_report(db: Session) -> list[dict]:
    """
    Query failed source runs and return their raw-event processing status.

    READ-ONLY: performs one SELECT. No inserts, updates, deletes, or processing.

    Returns a list of dicts, one per failed source_run, with these keys:
        source_run_id, source_name, status, started_at, finished_at,
        records_fetched, records_valid, quarantine_count, error_text,
        raw_event_count, events_with_evidence, events_without_evidence,
        needs_manual_review (bool — True when events_without_evidence > 0)
    """
    rows = db.execute(text(_FAILED_RUNS_SQL)).mappings().all()
    result = []
    for row in rows:
        raw_event_count = int(row["raw_event_count"] or 0)
        events_with_evidence = int(row["events_with_evidence"] or 0)
        events_without_evidence = raw_event_count - events_with_evidence
        result.append({
            "source_run_id": str(row["source_run_id"]),
            "source_name": row["source_name"],
            "status": row["status"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "records_fetched": int(row["records_fetched"]),
            "records_valid": int(row["records_valid"]),
            "quarantine_count": int(row["quarantine_count"]),
            "error_text": row["error_text"],
            "raw_event_count": raw_event_count,
            "events_with_evidence": events_with_evidence,
            "events_without_evidence": events_without_evidence,
            "needs_manual_review": events_without_evidence > 0,
        })
    return result


def print_report(rows: list[dict]) -> None:
    """Print the diagnostic report to stdout. Does not modify any data."""
    sep = "=" * 72
    print(sep)
    print("FAILED SOURCE RUN DIAGNOSTIC REPORT")
    print("READ-ONLY: no records were recovered, processed, or modified.")
    print(sep)

    if not rows:
        print("No failed source runs found.")
        print(sep)
        return

    for row in rows:
        print(f"\nSource Run ID      : {row['source_run_id']}")
        print(f"Source             : {row['source_name']}")
        print(f"Status             : {row['status']}")
        print(f"Started            : {row['started_at']}")
        print(f"Finished           : {row['finished_at']}")
        print(f"Error              : {row['error_text']}")
        print(f"Records Fetched    : {row['records_fetched']}")
        print(f"Records Valid      : {row['records_valid']}")
        print(f"Quarantine Count   : {row['quarantine_count']}")
        print(f"Raw Events in DB   : {row['raw_event_count']}")
        print(f"  with evidence    : {row['events_with_evidence']}")
        print(f"  WITHOUT evidence : {row['events_without_evidence']}")
        if row["needs_manual_review"]:
            print(
                f"\n  *** ACTION REQUIRED ***\n"
                f"  {row['events_without_evidence']} raw event(s) were stored but never"
                f" processed into evidence, companies, signals, or scores.\n"
                f"  A separate recovery plan must be approved before these records\n"
                f"  can be processed. This script does NOT perform that recovery."
            )
        else:
            print("  All raw events have been extracted to evidence (no gap).")

    print(f"\n{sep}")
    needs_review = sum(1 for r in rows if r["needs_manual_review"])
    total_orphaned = sum(r["events_without_evidence"] for r in rows)
    print(f"Summary: {len(rows)} failed run(s) found.")
    print(f"         {needs_review} run(s) have unprocessed raw events.")
    print(f"         {total_orphaned} total raw event(s) without downstream evidence.")
    print("This report is READ-ONLY. No records were modified.")
    print(sep)


if __name__ == "__main__":
    from app.db.session import SessionLocal

    print("Connecting to database...")
    try:
        with SessionLocal() as db:
            report_rows = build_report(db)
            print_report(report_rows)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
