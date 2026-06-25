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

## Build status

Last committed: 2026-06-25 — feat: SBA 7(a) loan connector — PIF alumni and active loan signals

| Component               | Status                                                     |
|-------------------------|------------------------------------------------------------|
| FastAPI read-only layer | Built, validated, committed to git                         |
| Next.js frontend shell  | Built, committed — not authenticated, not production ready |
| tests/test_api.py       | Built, validated, committed to git                         |
| scoring_configs         | Seeded via migration 003. Hot threshold: 70. Live DB row updated to phase1-v1-corrected with full config JSON (was empty {}). Fresh deployments work without manual intervention. |
| Stale lead rescore      | scripts/rescore_stale_leads.py — one-off run on 2026-06-22 moved 2 companies (CAPITAL BRAND GROUP LLC, VETERAN TECHNOLOGY PARTNERS LLC) from warm→hot at score=73. Script inserts new versioned lead_scores rows; never updates historical rows. duplicate_active gate intentionally bypassed (data correction, not pipeline re-run). |
| Scoring — award signals | _AWARD_SIGNAL_TYPES = frozenset({"CONTRACT_AWARD","SUBCONTRACT_AWARD"}) in scoring.py. Both why_now (max 30) and ar_fit components now score SUBCONTRACT_AWARD signals. Previously only CONTRACT_AWARD was recognized, leaving subaward companies at why_now=0. Known gap: subaward companies still score low without NAICS enrichment — the subawards API returns no NAICS, UEI, or state, so the +15 NAICS bonus and +7 ar_fit NAICS bonus are always 0. Max realistic subaward score without NAICS is ~29 (archive tier). |
| Pipeline scheduling     | run_loop added to run_pipeline.py. PIPELINE_INTERVAL_HOURS env var controls interval (default 6h). Set to 0 for one-shot run. docker-compose restarts on crash. |
| USASpending subawards   | Second source built, committed. connector: app/pipeline/connectors/usaspending_subawards.py. claim_supported=SUBCONTRACT_AWARD. Source seeded enabled=False — enable after UAT of 20+ leads. API filters silently ignored; $1B amount cap + year 2000-2030 date guard in Pydantic validator. No UEI/NAICS/state in API response. Noise keyword filter (_NOISE_KEYWORDS / _SIGNAL_KEYWORDS) quarantines CCDBG childcare/social-service records; annotates signal matches in payload["description_signal_keyword"]. Sort: amount desc (not id desc — id desc surfaced CCDBG batch at top, 99.5% noise). USASPENDING_SUBAWARDS_START_PAGE=500 (default) skips $1B+ corrupt rows at pages 1-499. Live test at page 500: 186/200 valid (93%), 14 quarantined. |
| ICP Sector Soft-Flag    | Phase 2B Feature 1. Confirmed by: John Cox Miller, Porter Capital, June 24 2026. Excluded sectors: 11 Agriculture, 22 Utilities, 23 Construction, 52 Finance and Insurance, 61 Educational Services, 62 Health Care and Social Assistance, 71 Arts/Entertainment/Recreation, 92 Public Administration. Approach: soft flag only — DO NOT hard block. Leads scored and stored normally. sector_excluded=True hidden from sales by default. Visible via "Include leads outside Porter ICP sectors" toggle for researchers. Phase 4 AI will re-evaluate using John's feedback as training examples. Implementation: migration 005_add_sector_excluded_flag adds two columns to lead_candidates; flag_excluded_sector() in gates.py sets them; called from score_company() in scoring.py after candidate creation. 151 existing leads flagged (149 Construction 23x, 2 Health Care 62x). 1193 clean ICP leads remain. |
| USASpending NAICS expansion | Confirmed by John Cox Miller, Porter Capital, June 24 2026. _TARGET_NAICS_PREFIXES in app/pipeline/connectors/usaspending.py expanded from list to frozenset. Removed: 23 (Construction — now soft-excluded). Added: 21 Mining, 44/45 Retail Trade, 51 Information, 55 Management of Companies, 72 Accommodation and Food Services, 81 Other Services. Serialized as sorted() list in _fetch_page body (frozenset is not JSON-serializable). 731 tests passing. 5-page test pipeline confirmed new sectors produce leads: 51 Information (4 companies), 72 Accommodation/Food (2), 81 Other Services (3). No conflict with subawards connector — that endpoint ignores NAICS filters entirely. |
| SBA 7(a) loan connector     | Phase 2B. Confirmed by: John Cox Miller, Porter Capital, June 25 2026. Third lead source — ALL B2B industries (exclude only pure B2C: retail 44/45, restaurants/hotels 72). connector: app/pipeline/connectors/sba_loans.py. Two signal types: SBA_LOAN_PIF (paid-in-full, strong prospect) and SBA_LOAN_ACTIVE (active lien, needs qualification). Source seeded enabled=False via migration 006 — enable via SBA_LOANS_ENABLED=true after UAT. CSV cached at data/sba_loans_cache.csv (30-day TTL, ~150 MB). Read in 1000-row chunks. Filters: AL/GA/TN/FL/MS/TX/VA, $50K–$5M, 2022-01-01+, skip CHGOFF/CANCLD/EXEMPT. Scoring: SBA_LOAN_PIF +8 why_now, SBA_LOAN_ACTIVE +4 why_now (capped at 30). Freshness window: 1825 days (5 yr) so 2022 loans clear Gate 3 (floor 0.1). Frontend: "✓ SBA Alumni" (green) and "⚠ Active SBA" (amber) source badges. 798 tests passing (51 new). UAT test run 2026-06-25 (pipeline_run_id=94111be5): 150K rows read, 14,454 passing all filters (135,546 skipped), 676 SBA_LOAN_PIF + 13,778 SBA_LOAN_ACTIVE signals, 0 parsing errors, 0 overlap with USASpending companies (entirely new leads). Tier breakdown: cold=4,659 / archive=8,531 / warm=0 / hot=0. Max score=43 (cold) — SBA signals alone won't reach warm (score needs 50+); gap is NAICS enrichment (+15 ar_fit bonus). New SBA CSV format change: data.sba.gov portal reorganized; URL and all Pydantic aliases updated to lowercase column names; LoanStatus now "P I F" (with spaces). Fix committed: 72d82ba. |
