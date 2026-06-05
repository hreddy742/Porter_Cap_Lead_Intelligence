# Porter Capital Lead Intelligence System
## Claude Code Project Memory — read this before every session

---

## What this system does
An internal pipeline that discovers B2B companies that may need A/R financing,
scores and classifies them as leads, routes the best ones through human review,
and pushes approved leads to Salesforce. Every lead is traceable to evidence.

## Six founding rules — never violate these
1. No score point without a citing `evidence_id` → raise `ScoringIntegrityError` if violated
2. No company merge without a hard identifier match (UEI / domain / state_entity_id only)
3. `review_decisions`, `deal_attribution`, `salesforce_sync_logs` are APPEND-ONLY — no UPDATE, no DELETE, no TRUNCATE
4. Mandatory gates run BEFORE point scoring. A gated lead gets no score number ever.
5. AI is never in any scoring, gating, or approval path.
6. Pydantic v2 only — `@field_validator` + `@classmethod`, never `@validator`

## Tech stack
- Python 3.12
- PostgreSQL 16
- SQLAlchemy 2.x (ORM)
- Alembic (migrations — every migration needs a working downgrade())
- Pydantic v2 (validation at connector boundary)
- Streamlit (dashboard)
- httpx (HTTP client — always set timeout=30.0)
- structlog (structured JSON logging)
- Sentry SDK (error monitoring)
- Docker Compose (local dev + production)
- pytest + testcontainers (tests)

## Project layout
```
app/
  db/          → SQLAlchemy models + session
  pipeline/    → orchestrator + connectors
  processing/  → evidence, resolution, signals, suppression, scoring
  dashboard/   → Streamlit app
  utils/       → normalize, hashing, logging helpers
tests/         → pytest test files
alembic/       → migrations
scripts/       → cron entrypoint, import scripts
```

## Build order — NEVER skip ahead
1. normalize_company_name() with all tests passing
2. Corrected Alembic migration (schema ships blockers fixed)
3. USASpending connector (paginated, Pydantic v2, retry)
4. Evidence extractor
5. Company resolution (hard-ID only auto-merge)
6. Signal detection
7. Suppression engine
8. Scoring engine (versioned, gated)
9. Streamlit dashboard + review actions
10. CSV export

## Schema ship-blockers already fixed in this codebase
- duplicate_review unique constraint → expression index (not table UNIQUE)
- lead_scores CHECK → cardinality(evidence_ids) > 0 (not array_length)
- Append-only tables → BEFORE trigger RAISE EXCEPTION (not RULE DO NOTHING)
- FK ordering → tables created first, ALTER TABLE ADD CONSTRAINT after
- Pydantic → @field_validator everywhere

## Normalizer rules (app/utils/normalize.py)
STRIP ONLY true legal suffixes: llc, inc, corp, corporation, ltd, limited, co, lp, llp, plc
DO NOT strip: group, holdings, services, solutions, enterprises, international, associates
Reason: stripping descriptive words causes false collisions on name+state lookup

## external_id rule
Computed ONCE at company creation, stored in companies.external_id (immutable).
NEVER recomputed from live identifiers — output changes as identifiers are learned.
Algorithm priority: UEI → domain → normalized_name+state

## Testing requirements before any module merges
- normalize: 15+ unit tests including "stays-distinct" (Acme Group ≠ Acme Services)
- entity resolution: deliberate false-merge test must pass (two similar names → two companies)
- scoring: determinism test (same input × 3 runs = identical score)
- append-only: UPDATE and DELETE on review_decisions must RAISE, not succeed silently
- migrations: upgrade → downgrade → upgrade must all succeed

## Never do these
- Use @validator (Pydantic v1) — always @field_validator
- Auto-merge companies on name similarity alone
- Recompute external_id from live identifiers
- Award score points without citing evidence_ids
- Put any business logic in Streamlit — it belongs in app/processing/
- Use array_length() for empty-array checks — use cardinality()
- Trust X-Forwarded-Email from client — strip it in Caddy before auth
