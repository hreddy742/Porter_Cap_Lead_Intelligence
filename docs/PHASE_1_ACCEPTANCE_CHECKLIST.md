# Phase 1 Acceptance Checklist
## Porter Capital Lead Intelligence

All items must be checked before Phase 1 is considered complete.

---

## Module completion

| Task | Module | Status |
|---|---|---|
| 01 | `app/pipeline/connectors/usaspending.py` — USASpendingConnector, Pydantic v2, pagination, dedup | COMPLETE |
| 02 | `app/processing/evidence.py` — extract_evidence, freshness score, quarantine | COMPLETE |
| 03 | `app/processing/resolution.py` — resolve_company_for_evidence, hard-ID only, fuzzy→DuplicateReview | COMPLETE |
| 04 | `app/processing/signals.py` — detect_signals_for_evidence, signal_strength classification | COMPLETE |
| 05 | `app/processing/suppression.py` — check_suppression, route classification, import_suppression_csv | COMPLETE |
| 06 | `app/processing/gates.py` — evaluate_mandatory_gates (9 gates, runs BEFORE scoring) | COMPLETE |
| 07 | `app/processing/scoring.py` — score_company, 6 components, ScoringIntegrityError | COMPLETE |
| 08 | `app/pipeline/orchestrator.py` — run_pipeline, source isolation, summary dict | COMPLETE |
| 09 | `app/dashboard/` — Streamlit review dashboard + review.py action helpers | COMPLETE |
| 10 | `app/export/csv_export.py` — export_approved_leads_csv, 19-column CSV | COMPLETE |
| 11 | `app/ops/health.py` — health report (latest run, source health, per-run summary) | COMPLETE |
| 12 | `scripts/smoke_demo.py` + `tests/test_smoke_demo.py` — end-to-end local smoke demo | COMPLETE |

---

## Test suite

| Requirement | Status |
|---|---|
| `pytest tests/test_normalize.py` — 30+ tests pass, including stays-distinct tests | PASS |
| `pytest tests/test_schema.py` — migration round-trip, append-only triggers, constraints | PASS |
| `pytest tests/test_usaspending.py` — pagination, Pydantic validation, quarantine | PASS |
| `pytest tests/test_evidence.py` — freshness score, claim support, quarantine edge cases | PASS |
| `pytest tests/test_resolution.py` — hard-ID merge, false-merge prevention | PASS |
| `pytest tests/test_signals.py` — signal detection, strength classification | PASS |
| `pytest tests/test_suppression.py` — suppression routes, CSV import | PASS |
| `pytest tests/test_gates.py` — all 9 gate conditions | PASS |
| `pytest tests/test_scoring.py` — determinism (same input × 3 = identical score), ScoringIntegrityError | PASS |
| `pytest tests/test_orchestrator.py` — source isolation, summary dict | PASS |
| `pytest tests/test_dashboard_review.py` — approve/reject/escalate actions | PASS |
| `pytest tests/test_csv_export.py` — 19-column output, approved-only filter | PASS |
| `pytest tests/test_health.py` — health report shapes | PASS |
| `pytest tests/test_smoke_demo.py` — smoke demo test harness | PASS |
| Total: 235+ tests | ALL PASS |

---

## Smoke demo

| Requirement | Status |
|---|---|
| `python scripts/smoke_demo.py` exits with code 0 | PASS |
| Demo company "Demo Contracting LLC" (UEI=DEMO0000000001) created | PASS |
| Evidence extracted with freshness_score ~0.81 | PASS |
| Company resolved via UEI hard-ID match | PASS |
| Signal detected (CONTRACT_AWARD) | PASS |
| Mandatory gates evaluated before scoring | PASS |
| Score ~68 / tier = warm produced | PASS |
| Review decision (action=approve) recorded | PASS |
| Transaction committed cleanly | PASS |
| Re-run is idempotent (duplicate_active gate, no duplicate rows) | PASS |

---

## CSV export

| Requirement | Status |
|---|---|
| `exports/demo_approved_leads.csv` created | PASS |
| File contains at least 1 row (the demo lead) | PASS |
| File has exactly 19 columns | PASS |
| Only approved leads appear (no rejected/pending rows) | PASS |

---

## Integrity rules verified

| Rule | Verified by |
|---|---|
| No score point without citing evidence_id — raises ScoringIntegrityError | `test_scoring.py` |
| No company merge on name similarity alone — hard-ID required | `test_resolution.py` (false-merge test) |
| review_decisions is append-only — UPDATE raises exception | `test_schema.py` |
| review_decisions is append-only — DELETE raises exception | `test_schema.py` |
| Mandatory gates run BEFORE scoring — gated leads get no score | `test_gates.py`, `test_scoring.py` |
| AI is never in any scoring, gating, or approval path | By design — no LLM calls anywhere in `app/` |
| Pydantic v2 @field_validator used throughout | Code review — no @validator usage |
| external_id computed once at creation, never recomputed | `resolution.py` code review |

---

## What Phase 1 does NOT include (by design)

These are intentional exclusions, not missing items:

| Item | Status |
|---|---|
| Salesforce push | NOT in Phase 1 |
| Scheduled cron job | NOT in Phase 1 |
| OAuth / Caddy authentication | NOT in Phase 1 |
| Paid enrichment (ZoomInfo, Clearbit, SAM.gov key) | NOT in Phase 1 |
| AI / LLM in scoring, gating, or approval | NOT in Phase 1 (founding rule) |
| Production deployment hardening | NOT in Phase 1 |
| Contact enrichment (contactability_score is always 0) | NOT in Phase 1 |

See `docs/PHASE_1_LIMITATIONS.md` for full detail.

---

## Sign-off

Phase 1 is complete when all items above are checked and `pytest -q` exits with 0 failures.
