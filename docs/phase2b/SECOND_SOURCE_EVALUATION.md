# Phase 2B — Second Lead-Discovery Source Evaluation

**Status:** Internal research / review-ready. Not sales-ready.
**Date:** 2026-06-16
**Author:** Engineering (Phase 2B planning)
**Scope:** Lead-discovery source expansion only. Not Salesforce, not paid enrichment,
not production automation, not sales handoff.

> **Honesty note.** This evaluation is based on the documented, publicly known
> characteristics of each source class. **No external APIs were called** to produce
> this report (no SAM.gov, no state portals, no scraping). Where a claim depends on a
> specific endpoint, dataset, or terms-of-service, that fact is flagged as
> *requires verification* and folded into the proposed small test plan (§17). Do not
> treat any "access method" line below as confirmed until the test plan's step 1
> (endpoint + terms check) is done.

---

## 1. Executive Recommendation

**Build next: State / local procurement (contract-award) data, starting with 1–3
pilot states that publish award data through an open-data API (Socrata/CKAN-style)
or a bulk CSV download.**

Rationale in one line: it is the **same proven thesis as USASpending** (a company just
won a slow-paying government contract → it has a fresh receivable and a working-capital
gap → A/R-finance fit), it is **free and public**, it is **testable on a tiny sample**,
its **legal risk is low**, and it maps onto the existing `CONTRACT_AWARD` evidence/signal
path **without touching scoring, gates, or suppression**.

**Highest signal-quality source overall is UCC filings** (a UCC-1 against a company is a
near-direct indicator of secured working-capital need and competitor-lien intelligence).
**But UCC is not recommended as the first build** because reliable bulk/API access is
mostly paid or scraping-dependent and is fragmented state-by-state. UCC is the strongest
**"evaluate first, build later"** candidate, gated on a paid-access/terms decision by Porter.

**Rejected as the first next source:** SAM.gov Opportunities API (it lists solicitations,
not awards — no company has won anything yet, and we are already rate-limited on SAM),
industry directories (a flat list with no "why-now" event and frequent anti-scraping terms),
and court/bankruptcy/distress feeds as a *discovery* source (access is paid/terms-restricted
and severe distress is anti-financeable; better used later as a risk/suppression input).

---

## 2. Current System Context

The pipeline today discovers companies from **USASpending** (federal contract awards),
extracts evidence, resolves companies on hard identifiers, detects signals, runs mandatory
gates, scores, and routes the best candidates to human review.

Relevant facts that constrain any new source (from the codebase, not assumptions):

- **One claim type is live today:** `evidence_items.claim_supported = "CONTRACT_AWARD"`
  (`app/processing/evidence.py`).
- **Scoring only counts `CONTRACT_AWARD` signals:** `app/processing/scoring.py` filters
  `signal_type == "CONTRACT_AWARD"`. Any new `signal_type` would be **ignored by scoring**
  unless scoring changes — and scoring changes are **out of scope** for this phase.
  → A new source that wants to score in 2B must reuse `signal_type = "CONTRACT_AWARD"`.
- **Auto-merge is hard-ID only** (founding rule 2): UEI, domain, or `state_entity_id`.
  Any other identifier a source provides (a state vendor number, a permit-holder ID) may be
  stored in `company_identifiers` but **must not** drive auto-merge.
- **Every signal/score point needs a citing evidence row** (founding rules 1 & 4). A source
  with no per-record permalink or stable record id is structurally weaker.
- **Latest controlled run (2026-06-15):** 3,505 companies resolved, 706 scored, 624 warm,
  82 cold, 794 quarantine. The system already has a meaningful federal-only universe; the
  goal of a second source is to **add a non-overlapping universe**, not re-cover federal.

---

## 3. Why SAM.gov Contactability Is Paused

Phase 2A used SAM.gov **Entity** validation as a contactability provider. SAM.gov's API
enforces strict rate limits, and we hit them during entity validation; the most recent
commit (`fix: harden SAM.gov rate limit handling`) added backoff but the practical
consequence is that **SAM needs cooldown time**. We have deliberately decoupled Phase 2A
SAM remediation from Phase 2B so that SAM rate-limit cooldown does **not** block source
expansion. This is also why SAM-based options below carry an explicit rate-limit penalty:
we have first-hand evidence that SAM throttles us.

---

## 4. Why Trucking / FMCSA Is Excluded

Trucking and freight factoring is handled by **Porter's sister company, Freight**. FMCSA
(carrier registrations, MCS-150, operating authority), trucking carrier databases, and
freight-specific lead sources are therefore **out of scope** for Porter Capital's next
source and are **not evaluated or recommended here**. This is a business-routing constraint,
not a data-quality judgment — FMCSA is otherwise a clean, free, API-accessible source, but
it points at the wrong company population for Porter Capital.

---

## 5. Evaluation Criteria

Each candidate is judged on:

| Criterion | What "good" looks like for Porter Capital |
|---|---|
| Business fit | Surfaces B2B/B2G companies that issue invoices and wait to be paid |
| Signal quality | A dated *event* implying a working-capital gap, not just a company listing |
| A/R-need indication | Maps to factoring / payroll funding / A/R / PO financing need |
| Data fields | Company name, address, identifier, amount, date, industry, state at minimum |
| Contactability fields | Phone / email / website (nice to have; usually a separate enrichment step) |
| Access method | API > bulk download/CSV > public portal > scraping > paid vendor |
| Legal / terms risk | Public records with permissive reuse beats ToS-restricted or FCRA-touching data |
| Rate-limit risk | Generous or token-gated APIs beat aggressive throttles (cf. SAM) |
| Implementation complexity | Single API beats 50 fragmented portals |
| Maintenance burden | Stable schema beats per-state hand-tuned parsers |
| Daily lead volume | Enough to matter, not so much it floods review |
| Dedup identifiers | Hard IDs (UEI/domain/state_entity_id) beat name+state only |
| Schema mapping | Reuses existing tables/claim cleanly = lower risk |
| Evidence permalink | Per-record citable URL exists |
| Small-sample testable | Can pull ~25–100 records without commitment |
| No paid vendor required | Can prove value before any spend |
| Verdict | Phase 2B build / later build / reject-as-first |

---

## 6. Candidate Source Comparison Table

| Source | Biz fit | Signal quality | A/R-need | Access | Legal risk | Rate-limit risk | Complexity | Free? | Small test? | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| **State / local procurement awards** | High | High (dated award) | Strong | API (Socrata) / CSV (varies by state) | Low (public records) | Low (token-gated) | Medium (fragmented) | Yes | Yes | **Phase 2B (build first)** |
| **UCC filings** | High | **Highest** (secured-debt event) | Very strong | Mostly paid bulk / portal / scraping | Medium (bulk often licensed) | Medium | High | Mostly no | Sometimes | Evaluate first, **build later** |
| **Secretary of State / registries** | Medium | Low for "why-now"; **high for identity** | Weak (no receivable yet) | API/bulk varies; some open data | Low | Low | Medium | Mostly yes | Yes | **Later** — use as identity/dedup enrichment, not discovery |
| **SAM.gov Opportunities API** | Low | Low (solicitation ≠ award) | Weak | API (key) | Low | **High** (we are throttled) | Low | Yes | Limited | **Reject as first** |
| **Construction / project awards (permits)** | High | Medium–High (new project) | Strong | Open-data portals / paid aggregators | Low (permits) / Medium (aggregators) | Low–Medium | Medium–High (fragmented) | Partly | Yes | **Later** (strong follow-on) |
| **Distress / lien / bankruptcy / court** | Mixed | High but double-edged | Mixed (mild=fit, severe=anti-fit) | PACER/paid/county portals | **High** (terms + FCRA) | Medium | High | Mostly no | Limited | **Reject as first** — later as risk/suppression input |
| **Industry directories (staffing/mfg/distribution)** | High vertical fit | **Low** (no event, no freshness) | Weak (list, not signal) | Scraping / paid license | **High** (anti-scrape ToS) | n/a | Medium | Mostly no | No | **Reject as first** — seed-universe at best |
| **(Other) Gov payment / "checkbook" data** | High | High (proves active invoicing) | Strong | Open data (some states) | Low | Low | Medium | Yes | Yes | Note / later (pairs well with procurement) |

---

## 7. Source Deep Dive: State Procurement / Contract Awards

**What it is.** State and large-local government contract awards — the state-level analogue
of USASpending. Many states and some big cities/counties publish awarded-contract and
vendor-spend datasets on open-data platforms (commonly Socrata, sometimes CKAN, sometimes
plain bulk CSV/Excel).

**Business fit — High.** A state or municipal agency is a slow payer (net-30 to net-90+,
often slower). A vendor that just won an award has a fresh receivable and frequently a
mobilization/working-capital gap before payment — the core factoring/A-R thesis. This is the
**same population logic** that already works for federal awards, extended to a non-overlapping
state/local universe.

**Signal quality — High.** A dated, amount-bearing award event with an agency counterparty.
Directly comparable to the existing `CONTRACT_AWARD` signal.

**A/R-need indication — Strong.** Government receivable + delivery obligation = classic
factoring / PO-financing setup.

**Data fields (typical, varies by state):** vendor/recipient name, vendor address,
award/contract amount, award or execution date, awarding agency/department, contract or
PO number, sometimes commodity/NAICS-like category, sometimes a **state vendor ID**.
Phone/email/website are **usually absent** (contactability stays a separate step).

**Access method.** Mixed and **must be verified per state** (test-plan step 1):
- Best case: Socrata Open Data API (JSON, app-token rate-gated) — e.g., many large states/cities.
- Common case: bulk CSV/Excel download refreshed periodically.
- Worst case: HTML portal only (defer those states).

**Legal / terms risk — Low.** Government contract awards are public records; open-data
platforms generally carry permissive reuse terms. Verify each portal's license line.

**Rate-limit risk — Low.** Socrata app tokens give generous limits; bulk CSV has none.

**Implementation complexity — Medium.** A single connector pattern works, but each state has
a different field schema → a per-state field-mapping table. Start with 1–3 states, not 50.

**Maintenance burden — Medium.** Field mappings drift when states revise datasets. Bounded if
we limit to a few high-value states initially.

**Expected daily lead volume.** Moderate-to-high per large state; controllable via date
windows. Easily throttled to avoid flooding review.

**Dedup identifiers.** Name + state always; sometimes a state vendor ID. **UEI is usually
absent** at the state level, and a state vendor ID is **not** in the hard-ID allowlist
(UEI/domain/state_entity_id) → most state-award companies will resolve on name+state and may
generate `duplicate_review` rows. This is acceptable but must be expected.

**Schema mapping.** Clean (see §19). Reuses `CONTRACT_AWARD`.

**`claim_supported`.** `CONTRACT_AWARD` (reuse — keeps scoring untouched; provenance preserved
via `source_id`).

**Evidence links.** Often yes (a contract/award detail permalink or a stable record id that
builds a permalink); for bulk CSV the dataset row id + dataset URL serves as the citation.
**Verify per state.**

**Small-sample testable.** Yes — pull ~25–100 award rows from one state read-only.

**Runs without paid vendors.** Yes.

**Verdict.** **Phase 2B — build first** (pilot 1–3 states).

---

## 8. Source Deep Dive: UCC Filings

**What it is.** Uniform Commercial Code financing statements (UCC-1/UCC-3) filed with each
state's Secretary of State when a creditor takes a security interest in a debtor's assets,
very often **accounts receivable / inventory / equipment**.

**Business fit — High.** This is the most on-thesis signal in the entire list. A UCC-1 against
a company means someone is already lending against its assets — that is a company that *uses*
secured working-capital financing. Two distinct plays: (a) **new filing** = active financing
need *right now*; (b) **existing filing** = competitor-lien intelligence (refinance/takeout
target, or a conflict to avoid). Collateral descriptions sometimes explicitly say
"accounts" / "receivables."

**Signal quality — Highest of any candidate**, *when accessible*.

**A/R-need indication — Very strong** and often explicit in the collateral description.

**Data fields.** Debtor name + address, secured party name + address, filing date, filing
number, filing type, collateral description, sometimes amendments/continuations. No
phone/email/website; rarely UEI/domain.

**Access method — the problem.** State-by-state SoS systems. Realistic options:
- Many states only offer **manual portal search** (no API, no bulk).
- Several states sell **bulk UCC data by paid subscription** (the common path for vendors).
- A few offer downloadable extracts.
- Commercial aggregators consolidate UCC nationally — **paid**.
- Scraping SoS portals is often **ToS-restricted** and brittle. Not approved.

**Legal / terms risk — Medium.** Filings are public, but *bulk* access is frequently licensed
and some states restrict bulk reuse. Must be checked per state / per vendor contract.

**Rate-limit / complexity / maintenance — Medium-to-High** due to fragmentation and
portal/format variance.

**Expected volume.** Potentially large, but gated entirely by access method.

**Dedup identifiers.** Debtor name + state, filing number. No hard ID for auto-merge → same
name+state resolution caveat as state procurement.

**Schema mapping.** Would need a **new claim** (e.g. `UCC_FILING`) and a **new signal_type**.
Because scoring only counts `CONTRACT_AWARD`, a UCC signal **would not score** without a
scoring change — explicitly **out of scope now**. So UCC also implies future scoring work,
reinforcing "later."

**`claim_supported`.** `UCC_FILING` (new — future).

**Evidence links.** Filing number + state search URL; permalink quality varies by state.

**Small-sample testable.** Only where a free portal/extract exists; otherwise needs a paid
trial first.

**Runs without paid vendors.** Mostly **no** at useful scale.

**Verdict.** **Evaluate first, build later.** Highest value, but blocked on a Porter decision
about paid bulk access/terms **and** a future scoring change. Strongest second-place candidate.

---

## 9. Source Deep Dive: Secretary of State / Business Registries

**What it is.** State business-entity registries: legal name, **state entity ID**, status
(active/dissolved), formation date, registered agent, address, sometimes officers.

**Business fit — Medium; mis-cast as a discovery source.** A new registration is a company
with *no receivables yet* — weak A/R "why-now." Its real value to *this* system is
**identity and deduplication**: `state_entity_id` is one of the three hard identifiers the
founding rules allow for auto-merge. So registries are a **first-class enrichment/resolution
input**, not a lead-discovery source.

**Signal quality — Low for "why-now," High for identity.**

**A/R-need — Weak** as a standalone trigger.

**Data fields.** Legal name, `state_entity_id` (**hard ID**), status, formation date,
registered agent + address, sometimes officers. No phone/email/website usually.

**Access.** Varies; some states publish bulk/open data, many are portal-only. Verify per state.

**Legal risk — Low** (public). **Complexity — Medium**, **Maintenance — Medium** (fragmented).

**Dedup identifiers.** **`state_entity_id` is a hard ID** — the standout value here. Would let
the system auto-merge correctly and reduce `duplicate_review` volume created by name-only
state-award matches.

**Schema mapping.** Best used to populate `company_identifiers` (`id_type='state_entity_id'`)
and enrich `companies`, not to create `signals`.

**`claim_supported`.** `BUSINESS_REGISTRATION` (identity claim) — not a scoring signal.

**Verdict.** **Later — as an identity/dedup enrichment companion to state procurement**, not as
the primary discovery source. (It directly fixes the name+state dedup weakness of §7.)

---

## 10. Source Deep Dive: SAM.gov Opportunities API

**What it is.** Federal contracting **opportunities** (solicitations / RFPs / notices) — what
the government *intends to buy*, before any award.

**Business fit — Low.** An opportunity has **no winning company** and therefore **no receivable
and no company-level A/R need**. It identifies demand, not a financeable counterparty. Using it
for leads would mean speculating about future bidders — out of thesis.

**Signal quality — Low** for Porter's purpose (it is upstream of any award).

**Rate-limit risk — High and demonstrated.** We are already throttled on SAM Entity validation
(see §3); adding SAM Opportunities concentrates more load on the same rate-limited host.

**Other.** Free, API-accessible, low legal risk — but those positives don't fix the fundamental
mismatch (no awarded company).

**Verdict.** **Reject as the next source.** Picking it merely because "SAM is already wired up"
is exactly the trap the brief warns against, and it doesn't produce A/R leads. Revisit only if a
specific bid-intelligence use case is defined later.

---

## 11. Source Deep Dive: Construction / Project-Award Sources

**What it is.** New-construction signals: building permits (city/county open data), public
construction-contract awards (a subset of procurement), and commercial project-start aggregators
(Dodge, ConstructConnect — paid).

**Business fit — High.** Construction is a core factoring / PO-financing vertical: subs and
suppliers carry 60–90+ day payment cycles, progress billing, and retainage. A new permit or
project award is a credible working-capital trigger.

**Signal quality — Medium-High.** A dated project start tied to a named contractor/permit holder.

**A/R-need — Strong** for subs/suppliers/specialty trades.

**Data fields (permits).** Permit holder / contractor name + address, valuation, issue date,
project type/description, sometimes contractor license number. No phone/email/website usually.

**Access.** Permits: many large cities/counties on open-data portals (Socrata-style) or bulk
CSV — **free**, but **highly fragmented** (per-municipality). Aggregators: **paid**.

**Legal risk — Low** for public permit data; **Medium** for licensed aggregators.

**Complexity / Maintenance — Medium-High** (even more fragmented than state procurement —
thousands of jurisdictions).

**Dedup identifiers.** Contractor name + state; sometimes a contractor **license number** (not a
hard ID for auto-merge). Same name+state caveat.

**Schema mapping.** Could reuse `CONTRACT_AWARD` for public construction awards (no scoring
change), or a `CONSTRUCTION_PROJECT` claim for permits (future, scoring change).

**Verdict.** **Later — strong follow-on after state procurement.** Same connector pattern, but
worse fragmentation; do it once the procurement pattern is proven. Flag paid aggregators clearly.

---

## 12. Source Deep Dive: Business Distress / Lien / Bankruptcy / Court Sources

**What it is.** Tax liens, judgments, lawsuits, and bankruptcy filings (federal PACER; state/
county courts; tax-lien records).

**Business fit — Mixed and double-edged.** *Mild* distress (a tax lien, a judgment, slow pay)
can indicate a company that *needs* factoring and can't get a bank line — genuinely on-thesis.
But *severe* distress (active bankruptcy) is typically **anti-financeable** — a factor will not
fund it. So the same feed produces both opportunities and disqualifiers; it is most useful as a
**risk / suppression** input, not a clean discovery trigger.

**Signal quality — High but ambiguous.** Requires careful severity classification.

**Access — poor / risky.**
- **PACER** (federal bankruptcy/courts): paid per page, and its terms **restrict bulk
  scraping/harvesting**. Not an approved scraping target.
- State/county court records: extremely fragmented; many portal-only; some paid.
- Tax-lien data: county-level or via paid vendors.

**Legal / terms risk — High.** PACER terms, plus a real **FCRA** concern: using
distress/derogatory data to make decisions *about offering credit* can pull the activity toward
consumer-/credit-reporting regulation. This needs Porter legal/compliance input before any build.

**Dedup identifiers.** Debtor name + state; case numbers. No hard ID.

**Schema mapping.** Would be a `DISTRESS_SIGNAL` claim feeding **suppression / risk**, not the
positive scoring path — and suppression changes are out of scope this phase.

**Verdict.** **Reject as the next source.** High legal/terms risk, fragmented/paid access, and an
ambiguous signal. Revisit later specifically as a **risk/suppression enrichment** input with
legal sign-off — not as discovery.

---

## 13. Source Deep Dive: Industry Directories

**What it is.** Vertical directories and association member lists for staffing, manufacturing,
distribution, and construction suppliers (e.g., generalist B2B directories and trade-association
rosters). Staffing is notable because **payroll funding** is a Porter product and staffing firms
are heavy factoring users.

**Business fit — High by vertical, but wrong shape.** Directories give a **list of companies in
a relevant industry** — a *target universe*, not a *dated event*. There is no "why-now," no
amount, no freshness. The whole pipeline is event/evidence-driven (founding rules 1 & 4); a flat
list has no citable signal to attach a score to.

**Signal quality — Low.** No event, no freshness.

**Access — risky.** Most directories carry **anti-scraping / no-bulk-reuse ToS**; legitimate
bulk use generally requires a **paid license**.

**Legal / terms risk — High** (ToS), absent a license.

**Dedup identifiers.** Name + state, sometimes website/domain (domain *is* a hard ID, a small
plus). But that doesn't fix the missing signal.

**Schema mapping.** No natural `signal`. At best a `companies` seed list to be *enriched* by an
event source later.

**Verdict.** **Reject as the next source.** Possibly useful much later as a **seed/target list**
that an event source (procurement, UCC) confirms — but on its own it produces no scorable lead
and carries ToS risk.

---

## 14. Recommended Second Source

**State / local procurement (contract-award) data — pilot with 1–3 states that expose an
open-data API or bulk CSV.**

Concretely, the recommended build is a `state_procurement` connector that:

1. Targets a small, pre-verified set of states (chosen in test-plan step 1 by access quality and
   contract volume), each via the lowest-risk access method available (API > bulk CSV).
2. Emits `raw_source_events` → `evidence_items(claim_supported='CONTRACT_AWARD')` →
   `signals(signal_type='CONTRACT_AWARD')`, reusing the existing extraction/resolution/gating/
   scoring path **unchanged**.
3. Registers itself in `source_registry` (disabled until ready; `legal_notes` populated as the
   `chk_enabled_requires_legal` constraint requires).

---

## 15. Why This Source Should Be Built First

- **Same proven thesis, new universe.** It extends the exact A/R logic that already works
  federally to state/local awards, capturing companies USASpending never sees.
- **Zero changes to scoring / gates / suppression.** By reusing `CONTRACT_AWARD`, it slots into
  the live scoring path. (A new `signal_type` would be silently ignored by `scoring.py` and would
  *force* a scoring change — which this phase forbids.)
- **Free and low legal risk.** Public records on open-data platforms with permissive terms.
- **Testable in small volume.** ~25–100 read-only rows from one state prove the mapping before
  any commitment.
- **Low rate-limit risk.** Socrata app tokens / static bulk downloads — the opposite of SAM.
- **Connector pattern is reusable.** The same shape later powers construction permits (§11).

---

## 16. Why Other Sources Should Wait

- **UCC filings** — highest signal, but access is mostly paid/scraping/fragmented **and** needs a
  future scoring change for a new signal type. Decision-gated on Porter paid-access/terms. Build
  *after* procurement, likely as Phase 2C.
- **Secretary of State registries** — not a discovery source; bring it in as the
  **identity/dedup companion** (`state_entity_id` hard ID) that improves resolution for the
  state-procurement leads.
- **SAM.gov Opportunities** — wrong object (solicitation, not award) and shares our throttled SAM
  host. Rejected as first.
- **Construction permits/awards** — strong, but more fragmented than state procurement; do it once
  the pattern is proven (public construction awards can even reuse `CONTRACT_AWARD`).
- **Distress/court** — high legal/FCRA/terms risk and an ambiguous (often anti-financeable) signal;
  later, as a **risk/suppression** input with legal sign-off, not discovery.
- **Industry directories** — no dated event to score and frequent anti-scrape ToS; at most a future
  seed list.

---

## 17. Proposed Small Test Plan

**Goal:** prove that one state's award data is accessible, legally usable, and mappable — with
**no production writes and no scoring impact**.

1. **Access & terms check (offline/manual, no bulk pull).** For 3–5 candidate states, confirm:
   (a) an open-data API or bulk CSV of contract awards exists; (b) the dataset license permits
   reuse; (c) each record has a stable id / permalink for evidence. Pick the 1–2 cleanest.
   → *verify:* a written one-line access+terms note per chosen state.
2. **Sample pull (≤100 rows, read-only, throwaway).** Fetch a small recent award sample from the
   chosen state into a scratch file (not the DB). → *verify:* sample contains name, amount, date,
   agency, state, and a citable id/URL.
3. **Field-mapping spike (no DB writes).** Map sample fields to the `evidence_items.extracted_fields`
   shape used by USASpending (company_name, award_amount, action_date, state_code, naics if present,
   awarding_agency, source permalink). → *verify:* a mapping table + 3 worked example rows.
4. **Dedup reality check.** Estimate what fraction of sample companies carry a UEI vs. name+state
   only. → *verify:* a number; informs expected `duplicate_review` load.
5. **Dry-run integration design review.** Confirm the mapping reuses `CONTRACT_AWARD` end-to-end
   and triggers **no** change to scoring/gates/suppression. → *verify:* sign-off note in this doc's
   follow-up.

No step calls SAM.gov, runs Contactability-Lite, or runs the full pipeline.

---

## 18. Proposed Connector Scope (for a future, approved build)

**In scope (when approved):**
- `app/pipeline/connectors/state_procurement.py` — paginated fetch (httpx, `timeout=30.0`),
  Pydantic v2 boundary validation (`@field_validator`), retry/backoff, per-state field-mapping
  config.
- One `source_registry` row per state (or one parameterized source), `enabled=False` until vetted,
  `legal_notes` populated.
- Reuse of the existing evidence extractor pattern by emitting payloads whose keys the extractor can
  map to `CONTRACT_AWARD` (or a thin state-specific extractor that outputs the same
  `extracted_fields` contract).

**Explicitly out of scope (this phase, per the brief):**
- No new connector implementation yet (this doc is planning only).
- No scoring, gate, suppression, or Salesforce changes.
- No scraping.
- No paid vendors.
- No SAM.gov calls.

---

## 19. Database Mapping Plan

Reuses existing tables — **no schema migration required** for the recommended source.

| Table | What a state award maps to |
|---|---|
| `source_registry` | One row, e.g. `name='state_procurement_<ST>'`, `category='procurement'`, `access_method='api'\|'bulk'`, `cost_type='free'`, `legal_notes='<license>'`, `signal_types=['CONTRACT_AWARD']`, `enabled=False` until vetted. |
| `raw_source_events` | One row per award: `payload` = raw JSON, `company_name_raw` = vendor name, `source_record_id` = state contract/PO id, `source_url` = award permalink, `content_hash` = sha256 of normalized payload (feeds `uq_raw_event_dedup`). |
| `evidence_items` | One row: `claim_supported='CONTRACT_AWARD'`, `extracted_fields` = {company_name, award_amount, action_date, state_code, naics_code?, awarding_agency, source permalink}, `confidence_score` (API ~0.9 / bulk slightly lower), `freshness_score` from award date. |
| `companies` | Resolved/created; populate `state`, `naics_code` if present, `canonical_name`/`normalized_name`. `external_id` computed once (UEI→domain→name+state — usually name+state here). |
| `company_identifiers` | Store any state vendor id as `id_type='state_vendor_id'` **for reference only — NOT used for auto-merge** (not in the hard-ID allowlist). If a UEI happens to be present, store as `id_type='uei'` (hard ID, mergeable). |
| `signals` | One row: `signal_type='CONTRACT_AWARD'`, `signal_date`=award date, `award_amount`, `freshness_score`, **`evidence_id` required** (the row above). |
| `lead_candidates` | Created/updated through the existing resolution → gate → score path, unchanged. |

**Resolution caveat (be honest):** because state awards rarely carry UEI/domain, most companies
resolve on **name + state**, which can raise `duplicate_review` rows. That is by design (founding
rule 2 forbids name-only auto-merge) and is the main operational cost of this source. The §9
registry companion (`state_entity_id` hard ID) is the intended future mitigation.

---

## 20. Evidence Mapping Plan

- **`claim_supported`:** `CONTRACT_AWARD` (reused). Keeps the scoring contract intact.
- **Provenance:** distinguished by `evidence_items.source_id` / `signals.source_id` pointing at the
  state `source_registry` row — so we never lose "which source said this," even though the claim and
  signal_type strings match the federal ones.
- **`source_url` / evidence link:** the per-award permalink where available; for bulk-CSV states,
  the dataset row id + dataset URL as the citation. **Per-state availability must be verified
  (test-plan step 1)** — a state with no stable per-record citation is a weaker candidate because
  every signal/score needs a citing evidence row.
- **`confidence_score`:** ~0.9 for structured API data, slightly lower for bulk CSV that needs more
  normalization (final value set during the mapping spike).
- **`freshness_score`:** computed from the award/action date using the existing freshness window.

---

## 21. Gating / Scoring Impact

**None required, by design.**

- Gates (`app/processing/gates.py`) operate on company/evidence/signal shape and award amounts —
  all of which a state award provides. The **award-amount quality gate** (Gate 10) already applies
  and will correctly filter tiny state awards. No gate code changes.
- Scoring (`app/processing/scoring.py`) counts `signal_type='CONTRACT_AWARD'` — which is exactly what
  this source emits. No scoring code changes.
- Suppression is unchanged.

> If a *future* source (UCC, permits-as-new-claim, distress) introduces a **new** `claim_supported` /
> `signal_type`, that would require a scoring change to be counted — **explicitly deferred** out of
> this phase. The recommended source is chosen precisely to avoid that.

---

## 22. Risks and Mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Per-state schema fragmentation inflates maintenance | High | Limit to 1–3 vetted states; per-state mapping config; reusable connector pattern |
| Low hard-ID coverage → `duplicate_review` load grows | High | Expect it; later add SoS registry companion for `state_entity_id`; monitor duplicate volume |
| Some states are portal-only (no API/bulk) | Medium | Test-plan step 1 drops them before any build |
| Dataset license restricts reuse | Low–Medium | Verify license per state; record in `source_registry.legal_notes` |
| Missing per-record permalink weakens evidence | Medium | Require a citable id/URL in step 1; fall back to dataset-row citation only if acceptable |
| Overlap with federal awards double-counts a company | Low–Medium | Resolution dedups on hard ID where present; name+state otherwise routes to `duplicate_review` |
| Award amounts in different units/format | Medium | Normalize in the Pydantic boundary; validate in the mapping spike |
| Scope creep into scoring changes | Medium | Hard rule: reuse `CONTRACT_AWARD`; any new signal type is a separate, later phase |

---

## 23. Open Questions for Porter

1. **State priority:** which states matter most for Porter Capital's book (geography / existing
   client concentration)? That should drive the 1–3 pilot picks more than data convenience.
2. **UCC paid access:** is Porter willing to fund a bulk UCC subscription or aggregator trial? That
   single decision unblocks the highest-signal source.
3. **Duplicate-review capacity:** is the review team prepared for more `duplicate_review` items from
   name+state matches, or should we sequence the SoS `state_entity_id` companion first?
4. **Distress/FCRA appetite:** does Porter legal want to scope a compliant distress/risk feed later,
   or keep that category off the table entirely?
5. **Volume ceiling:** desired daily new-lead cap so a large state doesn't flood review.

---

## 24. Final Go / No-Go Recommendation

**GO — to plan and (separately) build a `state_procurement` connector for 1–3 vetted states,
reusing the `CONTRACT_AWARD` path with no scoring/gate/suppression changes.**

**Conditioned on:** completing test-plan §17 step 1 (access + terms + permalink verification) for the
chosen states **before** any connector code is written, and on explicit approval to start the build
(no build is started by this document).

**NO-GO (as the next source):** SAM.gov Opportunities, industry directories, and distress/court feeds.
**HOLD:** UCC filings (highest value; build next *after* procurement, pending a paid-access/terms
decision and a future scoring change) and Secretary-of-State registries (bring in as an
identity/dedup companion, not as standalone discovery).
