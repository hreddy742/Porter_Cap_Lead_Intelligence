# Phase 1 Runbook
## Porter Capital Lead Intelligence — local operations reference

---

## Working directory rule

Every command in this runbook must be run from inside the `porter-leads/` project root — the folder that contains `CLAUDE.md`, `docker-compose.yml`, and `pyproject.toml`.

```bash
pwd
# must end with /porter-leads   (Mac/Linux)
# or   C:\...\porter-leads      (Windows)
```

If a command fails with "no such file" or "module not found", this is almost always the cause.

---

## Daily / manual run sequence

For a typical manual pipeline run on local dev:

```
1. Start the database (if not already running)
2. Activate the virtual environment
3. Run the pipeline (or smoke demo for testing)
4. Review leads in the dashboard
5. Inspect the CSV export
```

---

## How to start the database

```bash
docker compose up db -d
```

Check it is healthy before running anything else:
```bash
docker compose ps
# db   ...   Up   (healthy)
```

Stop the database:
```bash
docker compose down
```

Stop and wipe all data (destructive — use only to reset local dev):
```bash
docker compose down -v
```

---

## How to activate the virtual environment

**Windows:**
```powershell
.venv\Scripts\activate
```

**Mac / Linux:**
```bash
source .venv/bin/activate
```

You should see `(.venv)` in your prompt. All subsequent commands run inside the venv.

---

## How to run the test suite

Run all tests (requires Docker for the `db`-marked tests):
```bash
pytest -q
```

Run only unit tests (no Docker required):
```bash
pytest -q -m "not db"
```

Run a specific test file:
```bash
pytest tests/test_scoring.py -v
```

Run normalizer tests (no Docker, fastest sanity check):
```bash
pytest tests/test_normalize.py -v
```

Expected output when all tests pass:
```
235 passed in X.XXs
```

---

## How to run the smoke demo

The smoke demo proves the full Phase 1 chain works end-to-end with deterministic sample data. It requires the database to be running and the migration to have been applied.

```bash
python scripts/smoke_demo.py
```

Expected final output:
```
==============================================================
  SMOKE DEMO PASSED
==============================================================
  raw_event_id        : <uuid>
  evidence_id         : <uuid>
  company_id          : <uuid>
  signal_id           : <uuid>
  lead_candidate_id   : <uuid>
  total_score / tier  : 68 / warm
  review action       : approve
  CSV path            : exports/demo_approved_leads.csv
==============================================================
```

The demo is idempotent — re-running it detects existing rows and reuses them. On a re-run, you will see `gate_reason = duplicate_active` at step [6/7]; this is expected and the demo still passes.

---

## How to run database migrations

Apply all pending migrations:
```bash
alembic upgrade head
```

Roll back to a clean state:
```bash
alembic downgrade base
```

Upgrade → downgrade → upgrade round-trip (verifies migration correctness):
```bash
alembic upgrade head && alembic downgrade base && alembic upgrade head
```

Verify tables exist:
```bash
docker compose exec db psql -U porter -d porter_leads -c "\dt"
# should list 13 tables
```

---

## How to run the dashboard

```bash
streamlit run app/dashboard/app.py
```

Opens at http://localhost:8501.

In Phase 1 there is no authentication. The dashboard reads the local database directly. Streamlit may print a warning about `X-Forwarded-Email` — this is harmless in local dev (see troubleshooting below).

---

## How to inspect the exported CSV

After running the smoke demo or a real pipeline run:

```bash
# View column headers
head -1 exports/demo_approved_leads.csv

# Count rows
wc -l exports/demo_approved_leads.csv

# View full file (small datasets)
cat exports/demo_approved_leads.csv
```

The CSV has 19 columns:
`lead_candidate_id`, `company_id`, `canonical_name`, `external_id`, `uei`, `domain`, `naics_code`, `state`, `total_score`, `tier`, `evidence_ids`, `evidence_summary`, `signal_types`, `signal_strength`, `award_amount_usd`, `contract_start_date`, `reviewer_id`, `review_action`, `reviewed_at`.

Only leads with a `review_decision` of `action = "approve"` appear in the export.

---

## How to check health reporting

Health data is available from `app/ops/health.py`. In the dashboard, the Health tab surfaces this. To inspect it from a Python shell:

```python
from app.db.session import SessionLocal
from app.ops.health import get_latest_pipeline_health, get_source_health

db = SessionLocal()
print(get_latest_pipeline_health(db))
print(get_source_health(db))
db.close()
```

Fields returned by `get_latest_pipeline_health`:
- `has_runs` — False if no pipeline run exists yet
- `status` — last run status (completed / failed)
- `sources_total` / `sources_succeeded` / `sources_failed`
- `raw_events_processed`
- `errors` — list of failed source_run details

Note: `evidence_items_created`, `companies_resolved`, `signals_created`, `companies_scored` return `None` in Phase 1 — those counts are not captured per pipeline run yet.

---

## Troubleshooting

### "No module named app" / "ModuleNotFoundError"

The virtual environment is not activated, or you are not in the project root.

```bash
# Check working directory
pwd   # must end in porter-leads/

# Activate venv
source .venv/bin/activate   # Mac/Linux
.venv\Scripts\activate       # Windows

# Reinstall editable package if needed
pip install -e ".[dev]"
```

### "could not connect to server" / "connection refused" on port 5432

Docker is not running or the db container has not started.

```bash
docker compose up db -d
docker compose ps   # wait for "healthy"
```

### `alembic upgrade head` fails with "relation does not exist"

The database is not running. Start it first:
```bash
docker compose up db -d
# wait for healthy, then:
alembic upgrade head
```

### Alembic: "Target database is not up to date"

A migration is pending. Run:
```bash
alembic upgrade head
```

### pytest: "no tests ran" or "cannot import"

You are not in the project root, or the venv is not active. Both must be true for pytest to find the `app` package.

### Streamlit warning: "X-Forwarded-Email header missing"

This is expected in local dev. Phase 1 does not configure Caddy or OAuth. The dashboard falls back to a local unauthenticated mode. This warning does not affect functionality.

### pytest `db` tests are slow (~30 s)

Normal on first run — testcontainers pulls the `postgres:16` Docker image. Subsequent runs use the cached image and take ~10 s.

### Smoke demo: "gate_reason = duplicate_active" on re-run

Expected. The demo detects the existing lead_candidate and reuses it rather than creating a duplicate. This is correct idempotent behavior and the demo still passes.

### CSV export produces 0 rows

The review decision was not committed, or no lead has `action = "approve"` in the database. Re-run the smoke demo from scratch:
```bash
docker compose down -v
docker compose up db -d
alembic upgrade head
python scripts/smoke_demo.py
```

### Port 5432 already in use

Another Postgres instance is running on your machine. Either stop it, or change the port mapping in `docker-compose.yml` (e.g. `"5433:5432"`) and update `DATABASE_URL` in `.env` accordingly.
