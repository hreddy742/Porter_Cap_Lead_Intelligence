# Porter Leads — Staging Environment Runbook

## What staging is

A lightweight, isolated environment for testing the Porter Leads app, database,
dashboard, pipeline commands, backup/restore, and future changes without
touching the active local development database.

Staging uses a separate Postgres container (`db_staging`) on port **5433**,
a separate volume (`postgres_staging_data`), and a separate database
(`porter_leads_staging`). The dev database (`porter_leads` on port 5432) is
never touched by any staging command.

---

## What staging is NOT

- Not a production environment.
- Not a cloud deployment.
- Not a copy of production data.
- Staging must not push to Salesforce.
- Staging must not run paid enrichment.
- Staging must not run outreach.
- Staging must not contain real Porter customer suppression files unless
  specifically approved.
- Staging must not contain real secrets in committed files.

---

## Prerequisites

| Tool           | Check                          |
|----------------|-------------------------------|
| Docker Desktop | `docker --version`             |
| Python 3.12+   | `python --version`             |
| Alembic        | `alembic --version`            |

All commands below assume you are in the project root (`porter-leads/`).

---

## Setup

### 1. Copy the staging env template

```powershell
cp .env.staging.example .env.staging
```

The defaults work as-is for local staging. Do not commit `.env.staging`.

### 2. Start the staging database

```powershell
docker compose -f docker-compose.staging.yml up db_staging -d
```

Wait ~10 seconds, then confirm it is healthy:

```powershell
docker compose -f docker-compose.staging.yml ps
# db_staging   ...   healthy
```

### 3. Run Alembic migrations against staging

Set `DATABASE_URL` to the staging database, then run migrations:

```powershell
$env:DATABASE_URL = "postgresql://porter:localdev@localhost:5433/porter_leads_staging"
alembic upgrade head
```

Expected output:

```
INFO  [alembic.runtime.migration] Running upgrade  -> 001_phase1_initial, Initial Phase 1 schema
```

Verify tables were created:

```powershell
docker compose -f docker-compose.staging.yml exec db_staging psql -U porter -d porter_leads_staging -c "\dt"
```

### 4. Run the smoke check

```powershell
.\scripts\staging_smoke_check.ps1
```

The smoke check verifies:
- `db_staging` container is running and healthy
- `pg_isready` returns OK
- `alembic_version` table exists and has a row
- Core tables (`raw_source_events`, `evidence_items`, `companies`,
  `lead_candidates`, `source_runs`, `review_decisions`) exist
- Row counts are readable (zero is expected for a fresh staging DB)

No external API calls are made during the smoke check.

---

## Start the dashboard against staging

```powershell
docker compose -f docker-compose.staging.yml up dashboard_staging -d
```

Opens at **http://localhost:8502**.

The staging dashboard reads from `porter_leads_staging`. The dev dashboard
(if running) continues to use `porter_leads` on port 8501.

To stop the staging dashboard:

```powershell
docker compose -f docker-compose.staging.yml stop dashboard_staging
```

---

## Run a small pipeline test

> **WARNING: `python scripts/run_pipeline.py` calls the live USASpending API.**
> This is a real external HTTP call. Only run it against staging when you
> intentionally want to ingest a small amount of live data and have approval
> to do so. Do not run it to "just test the connection" — use the smoke check
> for connectivity testing instead.

If you have approval to run a small pipeline test against staging:

```powershell
$env:DATABASE_URL        = "postgresql://porter:localdev@localhost:5433/porter_leads_staging"
$env:PIPELINE_LOOKBACK_DAYS = "3"
python scripts/run_pipeline.py
```

`PIPELINE_LOOKBACK_DAYS=3` limits the query window to 3 days of awards,
which is a small, bounded fetch. The `.env.staging.example` defaults to 3.

Do not run with `PIPELINE_LOOKBACK_DAYS=30` or higher against staging without
review — that replicates the full dev/production run volume.

---

## Health check

If `app/ops/health.py` provides a health report script, point it at staging
by setting `DATABASE_URL` to the staging URL before running.

Current health report entry point (check with your team for the latest):

```powershell
$env:DATABASE_URL = "postgresql://porter:localdev@localhost:5433/porter_leads_staging"
python scripts/lead_quality_report.py
```

---

## Backup and restore for staging

The existing backup/restore scripts (`scripts/backup_db.ps1` and
`scripts/verify_restore.ps1`) are hard-coded for the `db` service and
`porter_leads` database. They are designed for dev and production use.

**Staging data is ephemeral.** For most staging workflows, simply tear down
and re-create the staging DB rather than backing it up. If you need to
snapshot staging data for debugging, you can adapt the backup script manually:

```powershell
# Example: manual backup of staging (not a supported script, reference only)
docker compose -f docker-compose.staging.yml exec -T db_staging `
    pg_dump -U porter -d porter_leads_staging -f /tmp/staging_backup.sql
docker compose -f docker-compose.staging.yml cp `
    db_staging:/tmp/staging_backup.sql backups/staging_backup.sql
```

The `verify_restore.ps1` script uses `porter_leads_restore_verify` as its
temporary verify database and always drops it afterwards. It does not touch
`porter_leads_staging`. You can run it against dev backups while staging is
running — they do not conflict.

---

## Tear down staging

Stop staging services (keeps the volume / data):

```powershell
docker compose -f docker-compose.staging.yml down
```

Stop staging and delete all staging data (volume destroyed):

```powershell
docker compose -f docker-compose.staging.yml down -v
```

After `down -v`, the next `up db_staging` + `alembic upgrade head` gives you
a completely fresh staging database.

---

## What not to do in staging

| Do not                                             | Reason                                      |
|----------------------------------------------------|---------------------------------------------|
| Set `DATABASE_URL` to `porter_leads`               | That is the active dev/production database  |
| Run `alembic upgrade head` without checking `$env:DATABASE_URL` | Risk of migrating the wrong DB |
| Commit `.env.staging`                              | Contains passwords and keys                 |
| Put real Salesforce credentials in `.env.staging`  | Staging must never push to Salesforce       |
| Run `PIPELINE_LOOKBACK_DAYS=30+` without approval  | Large live API call volume                  |
| Use production Sentry DSN in staging               | Pollutes production error tracking          |
| Load real customer suppression files               | Requires explicit approval                  |
| Run `docker compose -f docker-compose.staging.yml down -v` on a staging DB with unreplicated data | Data loss |

---

## Troubleshooting

### db_staging does not start

```powershell
docker compose -f docker-compose.staging.yml logs db_staging
```

Common causes:
- Port 5433 already in use: check with `netstat -an | findstr 5433`
- Docker Desktop not running

### alembic upgrade head targets the wrong database

Check what `DATABASE_URL` is set to before running:

```powershell
$env:DATABASE_URL
# Must show porter_leads_staging on port 5433, not porter_leads on port 5432
```

If blank, it falls back to whatever is in `.env`. Set it explicitly:

```powershell
$env:DATABASE_URL = "postgresql://porter:localdev@localhost:5433/porter_leads_staging"
alembic upgrade head
```

### Smoke check fails: tables missing

Migrations have not run yet. Run them first:

```powershell
$env:DATABASE_URL = "postgresql://porter:localdev@localhost:5433/porter_leads_staging"
alembic upgrade head
.\scripts\staging_smoke_check.ps1
```

### Smoke check fails: container not found

```powershell
docker compose -f docker-compose.staging.yml up db_staging -d
docker compose -f docker-compose.staging.yml ps
# Wait for healthy, then re-run the smoke check
```

### Dashboard at 8502 shows no data

This is expected for a fresh staging database with no pipeline run. Either:
- Run a small pipeline test (with approval — see above)
- Load sample data via `scripts/smoke_demo.py` with staging `DATABASE_URL` set

### Dev database was accidentally targeted

If you ran `alembic upgrade head` or `python scripts/run_pipeline.py` without
setting `DATABASE_URL` to the staging URL, the command ran against the dev
database. The dev database is not harmed by additional migrations (they are
idempotent via Alembic) or by a pipeline run (it appends data). Review what
ran and confirm the dev database is in the expected state with:

```powershell
docker compose exec db psql -U porter -d porter_leads -c "SELECT count(*) FROM source_runs ORDER BY started_at DESC LIMIT 5;"
```
