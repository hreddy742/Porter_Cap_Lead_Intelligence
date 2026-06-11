"""One-shot script: find and reset any stuck pipeline_runs with status='running'."""
from app.db.session import SessionLocal
from sqlalchemy import text

with SessionLocal() as db:
    rows = db.execute(
        text("SELECT id, started_at, status FROM pipeline_runs WHERE status = 'running'")
    ).fetchall()

    if not rows:
        print("No stuck runs found.")
    else:
        for row in rows:
            print(f"Stuck run: {row}")

        db.execute(
            text(
                "UPDATE pipeline_runs SET status = 'failed', ended_at = now(), "
                "error_summary = 'Reset by reset_stuck_run.py — interrupted run' "
                "WHERE status = 'running'"
            )
        )
        db.commit()
        print(f"Reset {len(rows)} stuck run(s) to status='failed'.")
