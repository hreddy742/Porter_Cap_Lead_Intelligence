# Porter Capital — Lead Intelligence System

Internal B2B lead pipeline that discovers companies winning federal contracts, scores them as potential A/R financing prospects, routes the best ones through human review, and exports approved leads to CSV (Phase 1) or Salesforce (Phase 2+).

Every score point is backed by a cited evidence record. No AI in the scoring path. No score without passing mandatory gates first.

---

## Phase 1 architecture

```
USASpending API (federal contract awards)
        │
        ▼
  raw_source_events      ← one row per award record ingested
        │
        ▼
  evidence_items         ← claim + freshness score, citing the raw event
        │
        ▼
  companies              ← resolved by hard ID (UEI / domain / name+state)
        │
        ▼
  signals                ← CONTRACT_AWARD, strength classification
        │
        ▼
  ── mandatory gates ──  ← 9 gates; fail = no score ever assigned
        │
        ▼
  lead_candidates        ← 6-component deterministic score, tier (hot/warm/cold)
        │
        ▼
  review_decisions       ← human approve / reject / escalate (append-only)
        │
        ▼
  exports/               ← 19-column CSV of approved leads
```

All business logic lives in `app/processing/`. The Streamlit dashboard (`app/dashboard/`) is display-only — it calls `app/dashboard/review.py` action helpers which live outside Streamlit.

---

## Prerequisites

| Tool | Version | Check |
|---|---|---|
| Python | 3.12+ | `python --version` |
| Docker Desktop | any recent | `docker --version` |
| uv (optional) | any | `uv --version` |
| Git | any | `git --version` |

---

## Setup — from zero to running tests

### 1. Clone / unzip

```bash
cd porter-leads   # must be inside this folder for all commands below
```

### 2. Copy .env

```bash
cp .env.example .env
```

Local defaults work as-is. Do not commit `.env`.

### 3. Create Python environment

**With uv (recommended):**
```bash
uv venv
uv pip install -e ".[dev]"
```

**With plain Python:**
```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Mac/Linux:
source .venv/bin/activate

pip install -e ".[dev]"
```

Verify:
```bash
python -c "import sqlalchemy, pydantic, streamlit; print('OK')"
```

### 4. Start the database

```bash
docker compose up db -d
```

Wait ~10 s, then confirm it is healthy:
```bash
docker compose ps
# db   ...   healthy
```

### 5. Run Alembic migrations

```bash
alembic upgrade head
```

Expected output:
```
INFO  [alembic.runtime.migration] Running upgrade  -> 001_phase1_initial, Initial Phase 1 schema
```

Verify 13 tables were created:
```bash
docker compose exec db psql -U porter -d porter_leads -c "\dt"
```

### 6. Run the test suite

```bash
pytest -q
```

All 235+ tests should pass. Tests that require Docker are marked `db` and will spin up a temporary Postgres via testcontainers (~30 s first run, ~10 s after).

### 7. Run the smoke demo

Requires Docker (database running):
```bash
python scripts/smoke_demo.py
```

Expected last lines:
```
  SMOKE DEMO PASSED
  total_score / tier  : 68 / warm
  CSV path            : exports/demo_approved_leads.csv
```

### 8. Inspect the CSV export

```
exports/demo_approved_leads.csv
```

19 columns: `lead_candidate_id`, `company_id`, `canonical_name`, `external_id`, `uei`, `domain`, `naics_code`, `state`, `total_score`, `tier`, `evidence_ids`, `evidence_summary`, `signal_types`, `signal_strength`, `award_amount_usd`, `contract_start_date`, `reviewer_id`, `review_action`, `reviewed_at`.

### 9. Start the dashboard

```bash
streamlit run app/dashboard/app.py
```

Opens at http://localhost:8501. In Phase 1 there is no authentication — the dashboard reads the local database.

---

## Environment variables

See `.env.example` for all variables. Required for local dev:

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `postgresql://porter:localdev@localhost:5432/porter_leads` | Points at the Docker Compose db |
| `POSTGRES_PASSWORD` | `localdev` | Must match docker-compose.yml |
| `LOG_LEVEL` | `INFO` | Set to `DEBUG` for verbose output |

Salesforce, SAM.gov, Sentry, and OAuth variables are all empty in Phase 1 — leave them blank.

---

## Docker Compose services

| Service | Command | Notes |
|---|---|---|
| `db` only | `docker compose up db -d` | Local dev — start this first |
| All services | `docker compose up -d` | Runs db + app + dashboard |
| Run pipeline | `docker compose run --rm app python -m scripts.run_pipeline` | On-demand pipeline run |
| Run migrations | `docker compose run --rm app alembic upgrade head` | Inside container |

---

## Project layout

```
app/
  db/                ← SQLAlchemy models (models.py) + session
  pipeline/
    connectors/      ← USASpending connector (usaspending.py)
    orchestrator.py  ← run_pipeline(), source isolation, summary
  processing/
    evidence.py      ← extract_evidence(), freshness score
    resolution.py    ← resolve_company_for_evidence(), hard-ID only
    signals.py       ← detect_signals_for_evidence(), strength classification
    suppression.py   ← check_suppression(), import_suppression_csv()
    gates.py         ← evaluate_mandatory_gates() — 9 gates, runs BEFORE scoring
    scoring.py       ← score_company(), 6 components, ScoringIntegrityError
  dashboard/
    app.py           ← Streamlit UI (display only)
    review.py        ← approve/reject/escalate action helpers
  export/
    csv_export.py    ← export_approved_leads_csv(), 19-column output
  ops/
    health.py        ← get_latest_pipeline_health(), get_source_health()
  utils/
    normalize.py     ← normalize_company_name()
tests/               ← pytest test files (one per module)
alembic/
  versions/
    001_phase1_initial.py  ← single migration, 13 tables
scripts/
  smoke_demo.py      ← Phase 1 end-to-end demo (deterministic sample data)
  run_pipeline.py    ← cron entrypoint (Phase 2)
exports/             ← CSV output land here (gitignored except .gitkeep)
docs/
  PHASE_1_RUNBOOK.md
  PHASE_1_ACCEPTANCE_CHECKLIST.md
  PHASE_1_LIMITATIONS.md
```

---

## Six founding rules (never violate)

1. No score point without a citing `evidence_id` — raises `ScoringIntegrityError`
2. No company merge without a hard identifier match (UEI / domain / state_entity_id)
3. `review_decisions`, `deal_attribution`, `salesforce_sync_logs` are append-only
4. Mandatory gates run before point scoring — a gated lead gets no score number
5. AI is never in any scoring, gating, or approval path
6. Pydantic v2 only — `@field_validator` + `@classmethod`, never `@validator`

---

## See also

- `docs/PHASE_1_RUNBOOK.md` — daily/manual run sequence and troubleshooting
- `docs/PHASE_1_ACCEPTANCE_CHECKLIST.md` — Phase 1 acceptance criteria
- `docs/PHASE_1_LIMITATIONS.md` — what Phase 1 intentionally does not do
