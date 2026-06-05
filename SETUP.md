# Porter Leads — Setup Guide
## From zero to running tests in 15 minutes

---

## What you need installed first

Before anything else, make sure you have these on your machine:

| Tool | Check | Install |
|---|---|---|
| Python 3.12+ | `python3 --version` | https://python.org |
| Docker Desktop | `docker --version` | https://docker.com |
| Git | `git --version` | https://git-scm.com |
| Claude Code | `claude --version` | See step 3 |

---

## Step 1 — Get the project folder onto your machine

If you downloaded this as a zip, unzip it and open a terminal in that folder.
If you're starting fresh from git:

```bash
git init porter-leads
cd porter-leads
# then copy all the files in
```

You should be inside the `porter-leads/` folder for every command below.

---

## Step 2 — Create your .env file

```bash
cp .env.example .env
```

For local development, the defaults work fine — you don't need to change anything yet.

---

## Step 3 — Install Claude Code

**Option A: Terminal (Claude Code CLI)**
```bash
npm install -g @anthropic-ai/claude-code
```
Then: `claude` in your terminal opens an AI coding session in your project.

**Option B: VS Code extension**
In VS Code → Extensions → search "Claude Code" → Install.

Both work. Use whichever feels natural.

---

## Step 4 — Start the database

```bash
docker compose up db -d
```

This starts a PostgreSQL 16 database. Wait ~10 seconds, then check it's healthy:

```bash
docker compose ps
# Should show "db" with status "healthy"
```

---

## Step 5 — Install Python dependencies (local dev)

```bash
# Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate      # Mac/Linux
# .venv\Scripts\activate       # Windows

# Install everything
pip install -e ".[dev]"
```

Verify it worked:
```bash
python -c "import sqlalchemy; import pydantic; print('OK')"
```

---

## Step 6 — Run the database migrations

This creates all 13 tables with the correct schema:

```bash
alembic upgrade head
```

You should see output like:
```
INFO  [alembic.runtime.migration] Running upgrade  -> 001_phase1_initial, Initial Phase 1 schema
```

If it works, your database is ready.

To verify the tables were created:
```bash
docker compose exec db psql -U porter -d porter_leads -c "\dt"
```

You should see 13 tables.

---

## Step 7 — Run the normalizer tests (THE FIRST MILESTONE)

```bash
pytest tests/test_normalize.py -v
```

Expected output — all 30+ tests should pass:
```
tests/test_normalize.py::test_normalize_legal_suffixes[Acme Corp LLC-acme] PASSED
tests/test_normalize.py::test_normalize_legal_suffixes[ACME CORPORATION-acme] PASSED
...
tests/test_normalize.py::test_group_vs_services_stay_distinct PASSED      ← KEY TEST
tests/test_normalize.py::test_group_vs_holdings_stay_distinct PASSED      ← KEY TEST
...
====== 30 passed in 0.12s ======
```

**If these all pass: you have a working foundation. This is your first real milestone.**

---

## Step 8 — Run the schema tests (requires Docker)

```bash
pytest tests/test_schema.py -v -m db
```

This spins up a fresh Postgres with testcontainers, applies all migrations, and verifies:
- Migration round-trip (upgrade → downgrade → upgrade)
- UPDATE on review_decisions raises an exception
- DELETE on review_decisions raises an exception
- One-active-candidate-per-company constraint holds
- Evidence CHECK constraint correctly rejects empty evidence array

This takes ~30 seconds (testcontainers startup). All tests should pass.

---

## Step 9 — Start the dashboard (optional check)

```bash
streamlit run app/dashboard/app.py
```

Opens at http://localhost:8501. Shows placeholder screens — this is correct.
The dashboard fills in over Weeks 2-5 as modules are built.

---

## Step 10 — Open Claude Code and start building

In your terminal, in the project root:

```bash
claude
```

Claude Code reads `CLAUDE.md` automatically. The first session to run:

```
Read CLAUDE.md. Read app/utils/normalize.py and tests/test_normalize.py.
Confirm you understand the normalizer rules — especially why "group", "services",
and "holdings" must NOT be stripped. Now read the migration at
alembic/versions/001_phase1_initial.py and explain the five ship-blocker fixes
that are applied in it.
```

This orients Claude Code to the project before you ask it to write anything.

---

## Week 1 goals (days 1-5)

By end of week 1, you should have:

- [ ] `pytest tests/test_normalize.py` — all pass
- [ ] `pytest tests/test_schema.py` — all pass
- [ ] `alembic upgrade head` runs clean
- [ ] `alembic downgrade base && alembic upgrade head` both succeed
- [ ] Docker Compose brings up db + app cleanly
- [ ] Dashboard loads at localhost:8501

**Then Week 2 starts with the USASpending connector. Use Claude Code with:**
```
Read CLAUDE.md and app/db/models.py section on RawSourceEvent.
Build the USASpending connector in app/pipeline/connectors/usaspending.py.
Use Pydantic v2 (@field_validator, not @validator). 
Paginate the API. Always set timeout=30.0 on httpx calls.
Write the connector tests first (mocked API, paginated response, timeout, bad payload quarantine).
```

---

## Common problems

**`alembic upgrade head` fails with "relation does not exist"**
→ Database isn't running. Run `docker compose up db -d` first.

**`psycopg2` import error**
→ `pip install psycopg2-binary` (should already be in pyproject.toml — check pip install ran).

**Port 5432 already in use**
→ Another Postgres is running locally. Stop it, or change the port in docker-compose.yml.

**testcontainers tests skip**
→ Docker isn't running. Start Docker Desktop.

**`pytest tests/test_schema.py` is slow**
→ That's normal — testcontainers pulls the Postgres image on first run (~2 min). 
   Subsequent runs are fast (~10 sec).

---

## Project file overview

```
CLAUDE.md                   ← Claude Code reads this first every session
SETUP.md                    ← this file
pyproject.toml              ← Python dependencies
docker-compose.yml          ← runs db + app + dashboard
Dockerfile                  ← builds the app container
.env.example                ← copy to .env; never commit .env
alembic.ini                 ← Alembic config (reads DATABASE_URL from .env)
alembic/
  env.py                    ← Alembic runtime (auto-loads DATABASE_URL)
  versions/
    001_phase1_initial.py   ← THE migration (13 tables, all fixes applied)
app/
  db/
    models.py               ← all SQLAlchemy ORM models
    session.py              ← database connection
  utils/
    normalize.py            ← normalize_company_name() — build here first
  pipeline/
    connectors/             ← one file per data source (Week 2+)
  processing/               ← evidence, resolution, signals, scoring (Week 2-4)
  dashboard/
    app.py                  ← Streamlit app (Week 5)
tests/
  test_normalize.py         ← run first; must all pass before anything else
  test_schema.py            ← run after migration; verifies ship-blocker fixes
  conftest.py               ← shared fixtures (testcontainers DB)
scripts/
  run_pipeline.py           ← cron entrypoint (fills in Week 2)
```
