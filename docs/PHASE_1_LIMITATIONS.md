# Phase 1 Limitations
## Porter Capital Lead Intelligence — what Phase 1 intentionally does not do

This document exists so that anyone reading this codebase knows exactly what is and is not built, without having to dig through the code to find gaps.

---

## Data sources

**Single source: USASpending only.**
Phase 1 ingests federal contract award data from the USASpending.gov public API. No other source is connected. SAM.gov (entity registration), SEC EDGAR (filing data), state procurement portals, and commercial databases are not integrated.

**Demo data is synthetic.**
`scripts/smoke_demo.py` inserts a single deterministic fake company ("Demo Contracting LLC", UEI=DEMO0000000001) and a fake $2.5M award. It never calls the real USASpending API. Production use requires a live database populated by a real pipeline run, which in turn requires network access to `api.usaspending.gov`.

---

## Salesforce integration

**No real Salesforce push.**
The database schema includes `salesforce_sync_logs` (append-only) and `deal_attribution` tables as placeholders. The sync module itself is not built in Phase 1. Approved leads live in the local database and the CSV export only. Nothing is sent to Salesforce.

**No live Salesforce deduplication.**
In Phase 2+, before pushing a lead, the system will check whether the company already exists in Salesforce as an account or opportunity. This check does not happen in Phase 1.

---

## Scheduling and automation

**No scheduled daily cron.**
The pipeline runs on demand only — either via `python scripts/smoke_demo.py` (demo data) or `python -m scripts.run_pipeline` (real data). There is no cron job, Celery beat, or similar scheduler. `scripts/run_pipeline.py` is a cron entrypoint skeleton for Phase 2.

---

## Authentication and security

**No production auth yet.**
The Streamlit dashboard has no authentication in Phase 1. It runs on `localhost:8501` and trusts the local environment. There is no login, no session management, and no role-based access control.

**No Caddy / OAuth.**
`CLAUDE.md` documents that `X-Forwarded-Email` must be stripped by Caddy before auth (to prevent header spoofing). Caddy is not configured in Phase 1. The dashboard ignores `X-Forwarded-Email` entirely — it will emit a Streamlit warning about the missing header in local dev, which is harmless.

---

## Enrichment

**No contact enrichment.**
`contactability_score` is always 0 in Phase 1. The scoring config caps this component at `phase1_value: 0`. Contact data (phone, email, LinkedIn) requires a paid enrichment provider (ZoomInfo, Clearbit, Apollo, etc.) which is not integrated.

**No paid enrichment APIs.**
No ZoomInfo, no Clearbit, no SAM.gov API key required, no data broker calls. All data in Phase 1 is from the free USASpending public API.

---

## Scoring gaps

**Risk scoring is limited.**
`risk_clean_score` is always 0 in Phase 1 (`phase1_value: 0`). A full risk score would incorporate OFAC screening, credit signals, adverse media, and litigation history — none of which are available without paid data sources.

**Existing customer routing is classified but not acted on.**
The suppression engine detects leads that match existing Porter Capital customers (route = `existing_customer`) and correctly suppresses them from the lead pipeline. However, there is no mechanism in Phase 1 to forward these matches to account owners or to update the relevant Salesforce account. That routing action is Phase 2.

---

## Dashboard

**Dashboard is minimal.**
The Streamlit dashboard shows the review queue, allows approve/reject/escalate actions, and has a health tab. It does not have:
- Historical trend charts
- Per-reviewer performance metrics
- Batch approve/reject
- Email notifications on new leads
- Mobile-friendly layout

---

## Deployment

**Deployment hardening is Phase 2.**
The Docker Compose setup is for local development only. Phase 2 will add:
- Production Docker image with non-root user
- Secrets management (no plaintext passwords)
- Caddy reverse proxy with OAuth
- HTTPS
- Log aggregation
- Automated database backups
- Health check endpoint for uptime monitoring

---

## What is solid in Phase 1

Despite the above limitations, the following are production-quality by design:

- All score points cite evidence — `ScoringIntegrityError` enforced in code and tests
- Company merges require hard identifier match — false-merge prevention tested
- Append-only tables enforced by database BEFORE trigger — not application-layer logic
- Mandatory gates run before scoring — a gated lead can never receive a score number
- AI is absent from the entire pipeline — founding rule, not a goal
- Migration has a working `downgrade()` — round-trip tested in `test_schema.py`
- Scoring is deterministic — same input produces identical output across runs
- Smoke demo is idempotent — safe to re-run on the same database
