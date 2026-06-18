# Phase 2A — Completion Summary

> **Status:** Phase 2A is **review-ready / internal-review-ready** — it is **NOT sales-ready.**
> Date: 2026-06-16 · Branch: `feature/harsha-work`

---

## 1. Executive Summary

Phase 2A turned the Phase 1 pipeline output into an **internally reviewable lead set**: leads
are scored, gated on award quality (Gate 10), aggregated for human reading, exported as a
manual review pack, and lightly enriched with **entity-match** contactability data from SAM.gov.

A SAM.gov rate-limit defect was found and fixed: 429 throttles previously overwrote good rows
with false `not_contactable` status. The provider/orchestrator is now hardened so a throttle or
API error **never writes a contactability row**. Of the 14 bad rows from the original run, **7
were corrected earlier today and 7 remain stale**; remediation of the last 7 is blocked right
now because SAM.gov is hard-rate-limiting even at 1 request/minute.

Phase 2A produces leads suitable for **human research and qualification**, not for direct sales
outreach. SAM.gov proves only that an entity exists and matches — it does **not** provide a
verified decision-maker email or phone. No Salesforce push, no paid enrichment, and no ROI proof
exist yet.

## 2. What Phase 2A Completed

- **Gate 10 (award-quality gate)** added and applied before point scoring, with a reprocess path
  for existing leads and an impact report (`app/ops/gate10_reprocess.py`, `app/ops/gate_impact.py`).
- **Company award aggregation display** — largest single award, 90-day total, lifetime total,
  positive award count, most-recent award — surfaced in the dashboard and review pack.
- **Dashboard readability** improvements for human reviewers (`app/dashboard/`).
- **Before/after quality comparison report** (`app/ops/quality_comparison.py`,
  `scripts/phase2a_quality_comparison_report.py`).
- **Manual review pack export** (`app/ops/manual_review_pack.py`) — research-ready lead cards in
  CSV + Markdown, each carrying an explicit "not sales-ready / verify before outreach" caution.
- **Contactability-Lite foundation** + **SAM.gov provider** (entity match by UEI), with a
  single-company and batch runner (`scripts/contactability_lite.py`).
- **SAM.gov rate-limit hardening** — 429/`rate_limited`/`error` no longer overwrite rows.

## 3. Lead Quality Improvements Completed

- Leads now gate on **real award evidence** (Gate 10) before any score number is assigned,
  consistent with the founding rule "mandatory gates run before point scoring."
- Award context is **aggregated per company** so a reviewer sees dollar magnitude, recency, and
  count at a glance instead of raw rows.
- The **quality comparison report** quantifies the before/after effect of these changes.
- Every exported lead cites **evidence** (claim, confidence, source URL) — no score point exists
  without a citing evidence_id.

## 4. Gate 10 / Award Quality Status

- **Built and validated.** Gate 10 classifies leads by award quality (e.g. `single_award_pass`)
  and runs as a mandatory gate before point scoring.
- Reprocess + impact tooling exists (`gate10_reprocess.py`, `gate10_impact_report.py`) and is
  covered by tests (`tests/test_gate10_reprocess.py`, `tests/test_gate_impact.py`).
- A gated lead receives **no score number** — unchanged founding rule, still enforced.

## 5. Manual Review Pack Status

- **Built and validated** (`app/ops/manual_review_pack.py`, `tests/test_manual_review_pack.py`).
- Latest exports present in `outputs/manual_review/` (2026-06-15 runs; 50 leads exported from 61
  active, warm tier).
- Each pack header states plainly: **not sales-ready, contacts not verified, factoring need not
  proven, human review required before outreach.**
- All business logic lives in `app/ops/` — Streamlit holds none (founding rule respected).

## 6. SAM.gov Contactability Status

- SAM.gov lookup proves **entity match only** (UEI → registered entity). It does **not** yield a
  verified decision-maker email or phone.
- Contactability status (`contactable` / `partially_contactable` / `not_contactable` /
  `needs_paid_enrichment`) is a **rollup heuristic**, not a verified contact decision.
- **Original bad run** `ba078bc3-58eb-4fb3-a1e2-6c130f7f2cd6` (2026-06-15): 25 companies, 14 wrote
  false `error`/`not_contactable` rows due to 429 throttling.
- **State as of 2026-06-16:**
  - Original 14 bad → **7 corrected** earlier today, **7 still stale**.
  - **Remaining stale (7):** American Institutes for Research (`MCN6J5L6M3T4`), ENSCO Inc
    (`DP3FAQE2UGT3`), Environmental Restoration LLC (`MHMPN3F73NX3`), ITC Federal LLC
    (`JP1AJYKRSMC4`), LBYD Federal LLC (`EYE9YYATMP77`), The Medical College of Wisconsin
    (`E8VWJXMMUQ67`), TRAX International Corporation (`XDC4K4U6LDK6`).
  - **No new bad `not_contactable` rows** were written today — the hardening holds.
  - Remediation of the last 7 was **attempted and halted today**: company 1 immediately returned
    429 at 1 req/min, so per the stop-on-throttle rule the run was killed. The killed run did
    **not** commit (no run row, no overwrite). Deferred to a later window when SAM resets.

## 7. SAM Rate-Limit Lessons

- 15-second spacing worked; **10-second spacing triggered 429**. Even **1 req/min triggered 429**
  in the 2026-06-16 window — the limit is bursty/IP-scoped, not purely per-minute.
- Safe operating profile going forward:
  - `SAM_RATE_LIMIT_PER_MINUTE = 1`, **30s between companies**, **5-min pause every 4–5**.
  - **Stop immediately** on any 429/`rate_limited`; never retry the same company in a loop.
- **Design lesson (fixed):** a provider failure is not evidence about the company. 429/`error`
  must never be stored as `not_contactable` and must never overwrite a good row.

## 8. What Is Validated

- Gate 10 logic, reprocess, and impact reporting (tests pass).
- Manual review pack export content and cautions (tests pass).
- Quality comparison report (tests pass).
- SAM.gov provider rate-limit handling: 429/error returns a failure status and writes **no row**
  (verified against live DB — the 7 stale rows were not re-damaged by today's halted attempt).
- Append-only and core founding rules unchanged.

## 9. What Is Partially Validated

- **SAM contactability coverage:** only a subset of warm leads has been enriched, and 7 entity
  matches remain unconfirmed because of throttling.
- **Contactability status accuracy:** the rollup is heuristic and has not been validated against
  ground-truth reachability.

## 10. What Is Not Yet Built

- Salesforce push / sync (no outbound write to CRM exists).
- Paid contact enrichment (verified decision-maker email/phone).
- Phase 2B data sources (state procurement / FL FACTS / second source — evaluated only, see
  `docs/phase2b/`).
- ROI measurement / deal attribution proof.

## 11. What Is Deferred by Design

- **Verified contact data** is intentionally out of scope for Phase 2A — entity match first, paid
  verification later.
- **Remediation of the final 7 stale SAM rows** is deferred to a SAM-reset window using the safe
  spacing profile.
- **Phase 2B source ingestion** is deferred to the next phase (not started in this task).

## 12. Remaining Risks

- **7 stale `not_contactable` rows** persist; a reviewer could misread them as "confirmed
  uncontactable." Mitigation: documented here and excludable by `enrichment_run_id`.
- **SAM.gov throttling** is unpredictable; bulk enrichment is not currently feasible without
  hitting 429s.
- **Heuristic contactability** may over- or under-state reachability until paid verification.
- **No CRM/ROI loop** means lead value is unproven in dollars.

## 13. Phase 2A Final Status

**Phase 2A is substantially complete and internal-review-ready**, with one tracked, non-blocking
remediation outstanding (7 stale SAM rows, deferred to a SAM-reset window). It is **not
sales-ready**: contacts are unverified, factoring need is unproven, and no Salesforce push or paid
enrichment exists. No scoring, gating, suppression, or Salesforce logic was changed in this task.

## 14. Recommended Next Phase 2B Starting Point

1. **Drain the 7 stale SAM rows** first in a quiet SAM window (1 req/min, 30s spacing, stop on
   429) — small, safe, closes Phase 2A cleanly.
2. **Then begin Phase 2B source onboarding** using the evaluations already in `docs/phase2b/`
   (start with the highest-access, lowest-friction state procurement / second source).
3. Defer paid contact enrichment and Salesforce push until a source produces enough qualified,
   reviewer-approved leads to justify cost.

---

## Appendix — Per-step learning notes (this task)

**Step 1 — Read-only verification.**
- *Did:* queried the original run and current `company_contactability` state without any writes.
- *Why it matters:* confirms ground truth before touching SAM, avoiding blind re-runs.
- *Changed from before:* established that 7 of 14 were already fixed and 0 new bad rows exist.
- *Next:* target only the 7 remaining stale companies.

**Step 2 — Print commands, ask before apply.**
- *Did:* printed the exact 7 single-company `--apply` commands with safe spacing; asked approval.
- *Why it matters:* no SAM call happens without an explicit, scoped, approved plan.
- *Changed from before:* replaces the original unscoped `--limit 25 --apply` batch.
- *Next:* run only after approval.

**Step 3 — Controlled remediation (halted).**
- *Did:* ran company 1; it returned 429 immediately at 1 req/min, so the run was stopped.
- *Why it matters:* the stop-on-throttle rule prevents both bad data and SAM hammering.
- *Changed from before:* hardening meant the 429 wrote nothing — no row was re-damaged.
- *Next:* retry the 7 in a SAM-reset window.

**Step 4 — Post-halt verification.**
- *Did:* re-ran the read-only check; stale count still 7, no new run row, no new bad rows.
- *Why it matters:* proves the halted attempt was inert (rolled back, no overwrite).
- *Changed from before:* confirms the hardening behaves as designed against live data.
- *Next:* close out Phase 2A and schedule the final drain.

**Step 5 — Closeout.**
- *Did:* wrote this summary capturing validated/partial/not-built/deferred status.
- *Why it matters:* gives a clear, honest Phase 2A boundary before Phase 2B.
- *Changed from before:* Phase 2A now has a written, review-ready completion record.
- *Next:* start Phase 2B from the recommended entry point above.
