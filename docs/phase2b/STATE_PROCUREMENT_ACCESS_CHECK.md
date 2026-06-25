# Phase 2B — State Procurement Source Access Check

**Status:** Internal research / review-ready. Not sales-ready.
**Date:** 2026-06-16
**Author:** Engineering (Phase 2B source-access validation)
**Scope:** Source-access validation only. No connector built, no application code changed,
no scoring/gates/suppression/Salesforce changes, no full-pipeline run, no SAM.gov calls,
no Contactability-Lite apply, no scraping.

> **Method & honesty note.** Findings below come from **web search and reading official
> portal/FAQ pages only** — no portals were scraped and no contract records were harvested.
> Each state was checked against official `.gov` sources. Where a fact could not be confirmed
> from the official page (e.g. whether a dashboard exports CSV, whether a download URL is
> automatable), it is marked ***unverified — test-plan item*** rather than asserted. Primary
> sources are linked inline. This is access *validation*, not a build.

---

## 1. Executive Summary

Of the six candidates, only **Florida (FACTS)** is confirmed to expose true
**award/contract-level** records with every field Porter needs — awarded **vendor name**,
**amount**, **dates**, **agency**, **commodity/category**, a **stable contract ID (FLAIR
Contract ID)**, a **public per-contract permalink**, and a **CSV/Excel download** — all as
**public records, free, no API key, no scraping**. It maps cleanly to
`claim_supported = CONTRACT_AWARD` and is easily testable on 25–100 rows.

→ **Best pilot: Florida (FACTS).**
→ **Backup pilot: Texas (data.texas.gov, Socrata SODA API)** — the strongest *technical*
access (open API, app-token, huge volume), but the exact award/vendor dataset must be pinned
down first; its open datasets lean toward cooperative-contract *sales* and vendor *payments*
rather than a single clean "award event" table.

The other four are deferred or rejected as a *first* pilot:
- **Georgia** — its procurement registry (GPR) is **bid opportunities / solicitations only**,
  not awards; transparency data is aggregate vendor *payments*. **Reject for now.**
- **North Carolina** — vendor portal (eVP) and NC BIDS are **solicitation/bidding** oriented;
  a clean award export is **unverified**. **Pilot later.**
- **Tennessee** — the CPO "All Contracts Dashboard" is contract-level but appears to be a
  **view/dashboard**; CSV/API export is **unverified**. **Pilot later.**
- **Alabama** — business-attractive (Porter's home state) but technically the weakest: the open
  checkbook is **payment-level (cash basis), Excel/PDF export only, no API, no clean per-record
  permalink**, and it is *payments*, not *award events*. **Pilot later.**

No source recommended here requires scraping or paid access for the pilot.

---

## 2. Candidate State Comparison Table

| State | Award-level data? | Vendor name | Award date | Amount | Agency | Category/NAICS | Stable record ID | Public permalink | Access method | Paid? | Scrape needed? | 25–100 row test? | Recommendation |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **Florida (FACTS)** | **Yes** (contracts, grant awards, POs) | Yes | Begin/end dates (Yes) | Yes | Yes | Yes (commodity/service) | Yes (FLAIR Contract ID) | **Yes** (`ContractDetail.aspx`) | CSV/Excel download (no API) | No | No | Yes | **Pilot first** |
| **Texas (data.texas.gov)** | Partial (coop-contract *sales*, vendor *payments*) | Yes | Reporting period / payment date | Yes | Yes | Partial | Yes (Socrata row id) | Yes (dataset/row URL) | **Open API (Socrata SODA)** + CSV | No | No | Yes | **Backup pilot** |
| **Tennessee (CPO dashboard)** | Yes (active contracts) | Yes | *unverified* | Likely | Yes | *unverified* | Likely (Edison id) | *unverified* | Dashboard / portal (export *unverified*) | No | Possibly | *unverified* | Pilot later |
| **North Carolina (eVP/NC BIDS/IPS)** | Mostly solicitations; awards *unverified* | Partial | *unverified* | *unverified* | Yes | *unverified* | *unverified* | *unverified* | Portal (export *unverified*) | No | Possibly | *unverified* | Pilot later |
| **Georgia (GPR / open.georgia.gov)** | **No** (GPR = bids; transparency = payments) | Payments only | n/a (award) | Payments only | Yes | No | Payment row | Partial | Portal / transparency export | No | n/a | Payments only | **Reject for now** |
| **Alabama (open.alabama.gov)** | Payments + contracts list | Payee name | Payment date (not award) | Yes (payment) | Yes | No | *unverified* | Weak | Excel/PDF download (no API) | No | No | Yes (payments) | Pilot later |

*"unverified" = not confirmable from the official page in this pass; resolved in the test plan (§12).*

---

## 3. Alabama Source Check

- **Source name:** Open Alabama (state "checkbook" / transparency) + AlabamaBuys statewide contracts.
- **Official URLs:** `https://open.alabama.gov/` (checkbook, contracts.aspx), `https://www.alabamabuys.gov/` (statewide contract browse).
- **Awards / POs / bids / solicitations:** Primarily **vendor payments** (checkbook) and a
  statewide-contract browse. It is **payment-level (cash basis)**, not discrete award events.
- **Awarded vendor/company name:** Yes — payee name (recorded "exactly as it appeared on the check").
- **Award date:** Not an award date — **payment date** only.
- **Award amount:** Payment amount (Yes, but per-transaction, unaudited cash basis).
- **Agency/buyer:** Yes.
- **Commodity/category/NAICS:** No (not in checkbook).
- **Stable record ID:** *Unverified* (transaction rows; no documented stable per-record id/permalink).
- **Public evidence link/permalink:** Weak — query/report pages, no clean per-record permalink found.
- **Access method:** **Downloadable Excel/PDF** (large queries may be PDF-only). **No API.** Not scraping.
- **Terms/legal:** Public transparency site; no explicit reuse restriction found (verify). Contact: opengov@Comptroller.Alabama.Gov.
- **Rate-limit:** N/A (manual export).
- **Freshness:** Updated **Tuesday–Saturday mornings**.
- **Expected volume:** High (all state payments), but payment-level — would need heavy roll-up to approximate award events.
- **Schema fit:** Could feed `companies` (payee) but **no clean award `signal`** — payment data is a poor fit for an *event-driven* `CONTRACT_AWARD` signal.
- **claim_supported = CONTRACT_AWARD:** Not cleanly — it is payment, not award. Would need a different/weaker claim.
- **25–100 row test:** Yes (payments), but low value as an award signal.
- **Recommendation:** **Pilot later.** Business-attractive (Porter's home state) but technically the
  weakest of the six for *award-event* discovery: payment-level, no API, no per-record permalink,
  no category. Revisit after the Florida pattern is proven, and check whether AlabamaBuys exposes
  award-level contract data with stable IDs.

---

## 4. Georgia Source Check

- **Source name:** Georgia Procurement Registry (GPR); Open Georgia transparency.
- **Official URLs:** `https://doas.ga.gov/state-purchasing` (GPR/bids & contracts); `https://open.georgia.gov/` (transparency).
- **Awards / POs / bids / solicitations:** GPR is a **bid-opportunity / solicitation advertising
  system** ("lists bid opportunities from state and local government entities"). Open Georgia is
  **expenditure/payment and salary** transparency, not discrete award events.
- **Awarded vendor/company name:** GPR — not at award level (it advertises opportunities). Open
  Georgia — vendor *payments* only.
- **Award date / amount / agency:** Award date/amount not exposed as an award event; agency yes.
- **Commodity/NAICS:** Limited.
- **Stable record ID / permalink:** Solicitation IDs (GPR) / payment rows (Open Georgia); no clean
  award permalink.
- **Access method:** Public portals + transparency export. No clean award API/CSV found.
- **Terms/legal:** Public; standard transparency terms.
- **Freshness:** GPR is current for opportunities; Open Georgia periodic.
- **Expected volume:** N/A for awards.
- **Schema fit / claim_supported = CONTRACT_AWARD:** **No** — no award-event dataset that names the
  *winning* vendor with date+amount in one place.
- **25–100 row test:** Not for awards.
- **Recommendation:** **Reject for now.** Fails the brief's hard rule: do not recommend a
  solicitation-only source, and do not recommend a source that doesn't identify the winning vendor.
  Revisit only if a Georgia *award* dataset (not GPR opportunities, not payments) is located.

---

## 5. Florida Source Check

- **Source name:** Florida Accountability Contract Tracking System (**FACTS**), Dept. of Financial Services.
- **Official URL / portal:** `https://facts.fldfs.com/` (public search), FAQ: `https://myfloridacfo.com/factshelp/facts-faq`.
- **Awards / POs / bids / solicitations:** **Contracts, grant awards, and purchase orders** —
  i.e. **award-level**, exactly what Porter needs (not solicitations).
- **Awarded vendor/company name:** **Yes.**
- **Award date:** Yes — contract **Beginning/Ending dates** (begin/execution date → `signal_date`;
  confirm exact export header field in test).
- **Award amount:** **Yes.**
- **Agency/buyer:** **Yes.**
- **Commodity/category/NAICS:** **Yes** — Commodity/Service Type (contracts & POs).
- **Stable record ID:** **Yes** — **FLAIR Contract ID** (e.g. `97323`) plus Agency-Assigned Contract
  ID, Grant Award ID, and PO Number.
- **Public evidence link/permalink:** **Yes** — confirmed pattern
  `https://facts.fldfs.com/Search/ContractDetail.aspx?AgencyId=<id>&ContractId=<flair_id>`
  (publicly viewable; no login). A login page exists but is for agency contract *managers* entering
  data — public search/view/download does not require it (FAQ).
- **Access method:** **CSV/Excel download** of filtered search results ("Download Results"). **No
  documented public API.** Not scraping — it is an official export. *Whether the download URL is
  parametrizable for automation is a test-plan item;* worst case is a periodic filtered export.
- **Terms/legal:** Public Florida state records; site states it is intended to "capture and report
  accurate state contract information." Low risk; record license note during test.
- **Rate-limit:** N/A (download-based) → effectively no throttle risk (contrast with SAM.gov).
- **Freshness:** Updated as agency contract managers enter contracts (some lag possible per FAQ).
- **Expected volume:** Moderate-to-high (all FL state agency contracts/POs); easily windowed by date.
- **Schema fit (see §15):** Clean — reuses the USASpending `CONTRACT_AWARD` evidence/signal path.
- **claim_supported = CONTRACT_AWARD:** **Yes** (reuse — no scoring change needed).
- **25–100 row test:** **Yes** — a small dated filtered export, read-only, into a scratch file.
- **Recommendation:** **Pilot first.** The only candidate confirmed to satisfy every required field
  with a public permalink, stable ID, and free non-scraping access.

---

## 6. Tennessee Source Check

- **Source name:** TN Central Procurement Office (CPO) "All Contracts Dashboard" + Statewide Contract Listing (Edison Supplier Portal).
- **Official URLs:** `https://www.tn.gov/.../all-contracts-dashboard.html`; Edison: `https://hub.edison.tn.gov/...`.
- **Awards / POs / bids / solicitations:** **Active contracts** (award/contract-level) — good thesis fit.
- **Awarded vendor/company name:** Yes (contract supplier).
- **Award date / amount / agency:** Agency yes; date/amount **likely** present but **unverified** from
  the dashboard page (the page would not load fully in this pass).
- **Commodity/category/NAICS:** *Unverified.*
- **Stable record ID:** Likely an Edison contract id; *unverified.*
- **Public evidence link/permalink:** *Unverified* — dashboards often lack a per-record permalink.
- **Access method:** **Dashboard / portal** (possibly Power BI). **CSV/API export unverified** — this
  is the make-or-break question for TN. A third-party (publicaccountability.org) previously ingested
  CPO data via Edison, but that copy is stale (2020) and not an official source.
- **Terms/legal:** Public; standard state terms.
- **Freshness:** "Active contracts" implies current.
- **Expected volume:** Moderate.
- **Schema fit / claim_supported = CONTRACT_AWARD:** Yes *if* an export with vendor+amount+date+ID
  exists.
- **25–100 row test:** Only if export is confirmed.
- **Recommendation:** **Pilot later.** Promising contract-level content, but blocked on confirming a
  machine-readable export (CSV/API) and a stable per-record citation. If the dashboard exports clean
  CSV, TN could be promoted ahead of Texas as backup.

---

## 7. Texas Source Check

- **Source name:** Texas Open Data Portal (`data.texas.gov`, Socrata) + Texas Comptroller Open Data / CPA API.
- **Official URLs:** `https://data.texas.gov/`, `https://comptroller.texas.gov/transparency/open-data/`,
  `https://api-doc.comptroller.texas.gov/`. Example dataset: "DIR Cooperative Contract Sales Data FY10–FY25" (`data.texas.gov/.../w64c-ndf7`).
- **Awards / POs / bids / solicitations:** Mixed. The cleanest relevant datasets are **cooperative-
  contract vendor *sales*** and **vendor *payments***, rather than a single discrete "award event"
  table. (Solicitations live in the separate ESBD system.)
- **Awarded vendor/company name:** Yes (vendor/payee).
- **Award date:** Reporting period / payment date (proxy for activity date).
- **Award amount:** Yes (sales/payment amount).
- **Agency/buyer:** Yes.
- **Commodity/category/NAICS:** Partial (varies by dataset).
- **Stable record ID:** Yes — Socrata row identifiers.
- **Public evidence link/permalink:** Yes — dataset + row URL.
- **Access method:** **Open API (Socrata SODA)**, app-token rate-gated, plus CSV download and the
  Comptroller CPA API. Best *technical* access of the six. No scraping.
- **Terms/legal:** Open data portal with permissive reuse (confirm dataset license in test).
- **Rate-limit:** Low risk with a free Socrata app token.
- **Freshness:** Varies by dataset (monthly/periodic for sales & payments).
- **Expected volume:** Very high.
- **Schema fit:** Good, but the chosen dataset determines whether each row is a true "award event."
  Vendor *sales* under cooperative contracts is a reasonable A/R-activity proxy; vendor *payments*
  prove active invoicing but are downstream of award.
- **claim_supported = CONTRACT_AWARD:** Achievable, but semantics depend on dataset choice — may be
  closer to "contract sales/activity" than a discrete award. Decide during the test.
- **25–100 row test:** **Yes** — trivial via the Socrata API with `$limit=100`.
- **Recommendation:** **Backup pilot.** Best automation story and volume; the open task is selecting
  the single dataset that best represents an award/A-R event. If Florida's download proves awkward to
  automate, Texas's API may overtake it for a production connector.

---

## 8. North Carolina Source Check

- **Source name:** NC electronic Vendor Portal (eVP), NC eProcurement, NC BIDS; historically the Interactive Purchasing System (IPS).
- **Official URLs:** `https://evp.nc.gov/`, `https://eprocurement.nc.gov/`.
- **Awards / POs / bids / solicitations:** eVP/NC BIDS are **solicitation/bidding** oriented (vendors
  respond to solicitations). Award notices historically appeared in IPS; a clean **award export** is
  **unverified**.
- **Awarded vendor/company name:** Partial / unverified at award level.
- **Award date / amount:** *Unverified.*
- **Agency/buyer:** Yes.
- **Commodity/NAICS / stable ID / permalink:** *Unverified.*
- **Access method:** Portal; machine-readable **award** export unconfirmed. `data.nc.gov` exists but a
  procurement-award dataset was not confirmed in this pass.
- **Terms/legal:** Public; standard terms.
- **Freshness:** Current for solicitations.
- **Schema fit / claim_supported = CONTRACT_AWARD:** Only if an award dataset with winning vendor +
  amount + date is located.
- **25–100 row test:** Not until an award export is confirmed.
- **Recommendation:** **Pilot later.** Leans solicitation-side; defer until an official NC award
  dataset/export is found. Do not pilot a solicitation-only portal ahead of Florida.

---

## 9. Best Pilot State Recommendation

**Florida — FACTS (`facts.fldfs.com`).**

It is the only candidate confirmed to expose **award/contract-level** records with:
awarded **vendor name**, **amount**, contract **dates**, **agency**, **commodity/category**, a
**stable record ID (FLAIR Contract ID)**, a **public per-contract permalink**
(`ContractDetail.aspx?AgencyId=…&ContractId=…`), and a **CSV/Excel download** — **free, public
records, no API key, no scraping, no paid access**. It maps directly to
`claim_supported = CONTRACT_AWARD` (no scoring/gate/suppression change) and is safely testable on
25–100 rows.

---

## 10. Backup Pilot State Recommendation

**Texas — `data.texas.gov` (Socrata SODA API) + Comptroller Open Data.**

Best *technical* access (open API, app-token, low rate-limit, very high volume, stable row IDs,
permissive reuse). The one open question is **dataset selection** — choosing the table that best
represents an award/A-R event (cooperative-contract *sales* vs. vendor *payments*). Resolve that in
the test plan; if Florida's download proves hard to automate, Texas is the stronger production path.

*(Conditional:* if the Tennessee CPO dashboard turns out to export clean CSV with
vendor+amount+date+ID, TN becomes a credible alternative backup — verify in test-plan step 1.)

---

## 11. Why Other States Should Wait

- **Georgia — Reject for now:** GPR is solicitations/opportunities only; transparency data is
  aggregate payments. No award-event dataset that names the winning vendor with date+amount. Fails
  the brief's hard rules.
- **North Carolina — Pilot later:** eVP/NC BIDS are solicitation/bidding oriented; a machine-readable
  *award* export is unconfirmed.
- **Tennessee — Pilot later:** contract-level content exists, but export (CSV/API) and per-record
  permalink are unverified; it may be a view-only dashboard.
- **Alabama — Pilot later:** business-attractive home state, but technically weakest for *award
  events* — payment-level (cash basis), Excel/PDF only, no API, no clean per-record permalink, no
  category. Revisit AlabamaBuys for award-level contract data after the FL pattern is proven.

---

## 12. Proposed Small Pilot Test Plan

**Goal:** prove Florida FACTS is mappable to `CONTRACT_AWARD` with no production writes and no
scoring impact. (Run the same checks against Texas as the backup.)

1. **Access & export check (manual, no bulk pull).** On FACTS Advanced Search, confirm: (a) a date-
   windowed search returns contracts/POs; (b) "Download Results" yields CSV/Excel with column
   headers; (c) the exact field names for vendor, amount, begin date, agency, commodity, FLAIR
   Contract ID; (d) the `ContractDetail.aspx` permalink resolves publicly without login; (e) whether
   the download URL is parametrizable (automatable) or must be a periodic manual export. Record a
   one-line license/terms note. → *verify:* a column-header list + one working permalink.
2. **Sample export (≤100 rows, read-only, throwaway).** Pull a small recent window into a scratch
   file (not the DB). → *verify:* rows contain vendor, amount, begin date, agency, FLAIR ID, permalink.
3. **Field-mapping spike (no DB writes).** Map FACTS columns to the existing
   `evidence_items.extracted_fields` shape (company_name, award_amount, action_date=begin date,
   state_code='FL', naics/commodity, awarding_agency, source permalink). → *verify:* mapping table + 3
   worked example rows.
4. **Dedup reality check.** Estimate share of FACTS vendors carrying a UEI/FEID vs. name+state only. →
   *verify:* a number informing expected `duplicate_review` load.
5. **Integration design review.** Confirm the mapping reuses `CONTRACT_AWARD` end-to-end and triggers
   **no** change to scoring/gates/suppression. → *verify:* sign-off note appended here.

No step calls SAM.gov, runs Contactability-Lite, runs the full pipeline, or scrapes.

---

## 13. Proposed Connector Name and Scope

**Name (future, when approved):** `app/pipeline/connectors/fl_facts.py`
(state-procurement connector, Florida pilot). Generalize to `state_procurement` only after a second
state is added.

**In scope (when approved — NOT now):**
- Read FACTS contract/PO export rows (CSV/Excel, or parametrized download if step-1 confirms it).
- Pydantic v2 boundary validation (`@field_validator`), `httpx` `timeout=30.0`, retry/backoff if a
  download endpoint is used.
- Emit `raw_source_events` → `evidence_items(CONTRACT_AWARD)` → `signals(CONTRACT_AWARD)` via the
  existing extractor/resolution/gate/score path, unchanged.
- One `source_registry` row (disabled until vetted; `legal_notes` populated).

**Explicitly out of scope:** any connector code now; scraping; paid vendors; scoring/gate/
suppression/Salesforce changes; SAM.gov calls; full-pipeline runs.

---

## 14. Proposed Source Registry Entry

One `source_registry` row (data only — created when the build is approved, not now):

| Column | Value |
|---|---|
| `name` | `fl_facts_state_contracts` |
| `category` | `procurement` |
| `access_method` | `bulk` (CSV/Excel download; revise to `api` only if a parametrized endpoint is confirmed) |
| `status` | `planned` |
| `enabled` | `False` (the `chk_enabled_requires_status` + `chk_enabled_requires_legal` constraints require status=`enabled` and non-null `legal_notes` before enabling) |
| `cost_type` | `free` |
| `rate_limit_per_minute` | `NULL` (download-based) |
| `legal_notes` | "Florida public state-contract records via FACTS (DFS). Public records; reuse permitted. Verified <date>." |
| `signal_types` | `['CONTRACT_AWARD']` |
| `base_url` | `https://facts.fldfs.com/` |
| `auth_env_var` | `NULL` (no key required) |
| `notes` | "Pilot source — FL FACTS. Permalink: ContractDetail.aspx?AgencyId&ContractId. ID: FLAIR Contract ID." |

---

## 15. Proposed Evidence Mapping

| Table | FACTS → Porter mapping |
|---|---|
| `raw_source_events` | One row per FACTS contract/PO export row. `payload` = raw row JSON; `company_name_raw` = vendor name; `source_record_id` = **FLAIR Contract ID**; `source_url` = `ContractDetail.aspx?AgencyId=<id>&ContractId=<flair_id>`; `content_hash` = sha256 of normalized row (feeds `uq_raw_event_dedup`). |
| `evidence_items` | `claim_supported='CONTRACT_AWARD'`; `extracted_fields` = {company_name, award_amount, action_date=contract **begin date**, state_code='FL', commodity/category, awarding_agency, contract permalink}; `source_url` = permalink (satisfies "every signal/score needs a citing evidence row"); `confidence_score` ≈ 0.85 (bulk export, slightly below the API 0.9 used for USASpending); `freshness_score` from begin date. |
| `companies` | Resolve/create; `state='FL'`; `naics_code` only if FACTS commodity maps cleanly (else leave null). `external_id` computed once (UEI→domain→name+state; usually **name+state** here). |
| `company_identifiers` | FLAIR Contract ID is a **contract** id, not a company id — keep it on the evidence/raw event, **not** here. If a vendor **FEID** is present, store as `id_type='feid'` **for reference only — NOT auto-merge** (not in the UEI/domain/state_entity_id hard-ID allowlist). UEI rarely present. |
| `signals` | `signal_type='CONTRACT_AWARD'`; `signal_date` = contract begin date; `award_amount`; `freshness_score`; **`evidence_id` required**. |
| `lead_candidates` | Created/updated via the existing resolution → gate → score path, unchanged. |

**`claim_supported`:** `CONTRACT_AWARD` (reused — keeps scoring untouched; provenance preserved via
`evidence_items.source_id` / `signals.source_id` pointing at the FACTS `source_registry` row).

---

## 16. Risks and Mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| FACTS download not URL-parametrizable (no automatable endpoint) | Medium | Test-plan step 1 confirms; fallback = scheduled manual filtered export feeding the connector (still no scraping) |
| Low hard-ID coverage → `duplicate_review` load grows | High | Expected; resolve on name+state; quantify in step 4; later add a FL identity companion if needed |
| "Begin date" ≠ true award/execution date | Medium | Confirm exact date columns in the export header (step 1); pick the column closest to award/execution |
| FACTS entry lag (agencies enter contracts late) | Medium | Use a rolling window and re-pull; accept some freshness lag (mirrors USASpending behavior) |
| Commodity code ≠ NAICS | Low–Medium | Store commodity in `extracted_fields`; only populate `companies.naics_code` if a clean crosswalk exists |
| Overlap with federal awards double-counts a vendor | Low–Medium | Hard-ID dedup where present; name+state otherwise routes to `duplicate_review` |
| Scope creep into scoring/new-claim work | Medium | Hard rule: reuse `CONTRACT_AWARD`; any new claim/signal type is a separate, later phase |
| Picking the wrong Texas dataset (backup) | Medium | Defer Texas dataset choice to the test; do not enable until a clear award/activity table is chosen |

---

## 17. Final Go / No-Go Recommendation

**GO — to run the small access/mapping test plan (§12) against Florida FACTS first, Texas second.**
No connector is built and no application code changes until that test passes and you explicitly
approve a build.

- **Pilot first:** **Florida (FACTS)** — confirmed award-level data, all required fields, stable ID,
  public permalink, free non-scraping export, clean `CONTRACT_AWARD` mapping.
- **Backup:** **Texas (data.texas.gov, Socrata API)** — best automation/volume; pin the dataset first.
- **Pilot later:** Tennessee (confirm export), North Carolina (find award export), Alabama (weak
  award semantics despite home-state value).
- **Reject for now:** Georgia (solicitation/payment only — no award-event dataset naming the winner).

No SAM.gov calls, no Contactability-Lite apply, no full-pipeline run, no scraping, and no code changes
were made to produce this document.
