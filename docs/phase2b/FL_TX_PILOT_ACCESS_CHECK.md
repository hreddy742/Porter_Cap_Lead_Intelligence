# Phase 2B — Florida FACTS vs Texas Pilot Access Check

**Status:** Internal research / review-ready. Not sales-ready.
**Date:** 2026-06-16
**Author:** Engineering (Phase 2B pilot-source access validation)
**Scope:** Access validation only. No connector built, no application/schema/scoring/gates/
suppression/Salesforce changes, no full-pipeline run, no SAM.gov calls, no Contactability-Lite
apply, no scraping or bulk-record harvesting.

> **Method & honesty note.** Findings come from **web search and reading official `.gov` portal/
> FAQ pages plus Socrata dataset *metadata*** — no portal was scraped and no contract records were
> harvested. Where a fact could not be confirmed from the official page (notably whether an
> award-level portal exposes an automatable export), it is marked ***unverified*** rather than
> asserted. Primary sources are linked inline.

---

## 1. Executive Recommendation

**Pilot with Florida FACTS — but keep the automated connector build BLOCKED.**

The validation produced one decisive finding:

- **True award-level data on both sides lives in interactive web portals with *no documented
  API or bulk export*** — Florida FACTS *and* Texas's award-level source (the **LBB Contract
  Reporting** database). On the award grain, Texas has **no automation advantage** over Florida.
- **Texas's only API-accessible open data** (the Socrata "DIR Cooperative Contract Sales Data")
  is confirmed **vendor-sales / payment-level, not award-level** (≈10.7M line-item sales rows).
  It is the wrong grain for a clean `CONTRACT_AWARD` signal and would require a *new, different*
  claim type plus future scoring work (out of scope now).

So the API exists, but not for award data; the award data exists, but not via API — on *both*
sides. Given that, the better **pilot** is the source with the cleanest true-award evidence,
all required fields, a confirmed public per-record permalink, and a documented (manual) export:
**Florida FACTS.** Texas LBB is a close second on data but documents no export at all.

Because this phase forbids building a connector and the next step is a small manual pilot,
**Florida is GO for the pilot**. But automated production ingestion of FACTS is **not yet proven
feasible without scraping** (no API; the results export is a post-search Excel via an ASP.NET
WebForms page; URL-parametrizable automation is unverified). The connector build therefore stays
**BLOCKED** until FACTS export automation is confirmed — or until Porter explicitly accepts a
**scheduled manual export** feeding the connector.

---

## 2. Why This Validation Is Needed

The prior access check named Florida FACTS the best pilot and Texas the backup, on the
assumption that Texas "likely has cleaner Socrata/API access." That assumption needed testing
against two specific risks the brief calls out:

1. **Don't pick Florida just because it's cleaner** if its data can't actually be automated.
2. **Don't pick Texas just because it has an API** if the API data isn't really award-level.

Both risks turned out to be real and material, so the final pilot decision could not be made
responsibly without this field-level check.

---

## 3. Florida FACTS Validation

Sources: `https://facts.fldfs.com/Search/ContractAdvancedSearch.aspx`,
`https://myfloridacfo.com/factshelp/facts-faq`, contract detail example
`https://facts.fldfs.com/Search/ContractDetail.aspx?AgencyId=450000&ContractId=97323`.

| Question | Finding |
|---|---|
| Exact export/download mechanism | **Results-page "Download Results" → Excel/spreadsheet** (per FAQ). The "Download"/"Download Crosswalk" buttons on the *Advanced Search form* are for the commodity/service lookup and the FLID→UNSPSC crosswalk (`flid_to_unspsc_mappings_mfmp.xlsx`) — **not** the results export. The results export appears after running a search. |
| Filterable by date? | **Yes** — Advanced Search has a Begin/End date range (mm/dd/yyyy), plus Agency, Dollar-Value range, and Commodity/Service Type filters. |
| Automatable safely without scraping? | **Unverified / doubtful.** No API and no documented parametrizable bulk endpoint. The results export is a post-search download on an ASP.NET WebForms page; automating it likely needs form/session emulation, which crosses into scraping (prohibited). **Manual export is fine for a pilot; production automation is the open risk.** |
| Vendor/company name in export? | **Yes.** |
| Contract amount? | **Yes** (and a Dollar-Value filter exists). |
| Beginning/execution/award date? | **Yes** — contract Beginning/Ending dates (begin/execution → `signal_date`; confirm exact export column in pilot). |
| Agency? | **Yes.** |
| FLAIR Contract ID or other stable ID? | **Yes** — **FLAIR Contract ID** (e.g. `97323`), plus Agency-Assigned Contract ID, Grant Award ID, PO Number. |
| ContractDetail.aspx permalink works without login? | **Yes** — public, no login (login exists only for agency contract managers entering data). |
| Terms/license/public-records note? | Public Florida state-contract records; site is intended to "capture and report accurate state contract information." Low risk; record license note in pilot. |
| Supports a 25–100 row pilot? | **Yes** — a date-windowed search + manual Excel export, read-only. |

**Verdict:** True award-level evidence with every required field, a confirmed public permalink,
and a documented manual export. Pilot-ready manually; automation unproven.

---

## 4. Texas Validation

Sources: `https://comptroller.texas.gov/transparency/open-data/contracts.php`,
`https://www.lbb.texas.gov/Contract_Reporting.aspx` (award-level),
`https://data.texas.gov/` + Socrata metadata for `w64c-ndf7` (API-level).

Texas splits into **two distinct layers** that must not be conflated:

### 4a. LBB Contract Reporting database (award-level)
| Question | Finding |
|---|---|
| Dataset name | Legislative Budget Board (LBB) Contract Reporting — "master database of all state government contracts" meeting thresholds ($14k+ consulting/professional/construction; $50k+ goods/services). |
| Award/PO/payment level? | **Award/contract-level.** |
| Vendor name / amount / date / agency / contract ID? | **Yes to all** — searchable/sortable "by agency, vendor, subject, **award date**, and more"; contract amounts and contract IDs shown; "contract ID hyperlinks" suggest per-record pages. |
| Socrata/API/CSV export? | **None documented** — interactive search tool only. Same automation question as Florida. |
| Stable row ID / permalink? | Contract ID + likely per-contract hyperlink (***unverified*** as a stable permalink). |
| Login? | No — "All reported contracts are publicly available." |
| 25–100 row pilot? | Possible by on-screen search; export path ***unverified***. |

### 4b. data.texas.gov Socrata — "DIR Cooperative Contract Sales Data" (API-level)
| Question | Finding |
|---|---|
| Socrata API available? | **Yes** — SODA API, app-token rate-gated, CSV download. |
| Award / PO / payment level? | **Vendor-sales / payment-level** — individual monthly vendor sales transactions (≈10.7M rows, FY10–FY25), line-item quantities/unit prices/invoices. **Not** award-level. |
| Vendor name / amount / date / agency? | `vendor_name`, `purchase_amount`, `order_date`/`shipped_date`/`purchase_month`, `customer_name`. |
| Stable row ID / permalink? | `sales_fact_number` (row id) + dataset/row URL. |
| Contract number? | `contract_number` present (links a sale to a cooperative contract, but the row is a *sale*, not the award). |
| True award signal or spend signal? | **Spend/activity signal**, not an award event. |
| Maps cleanly to CONTRACT_AWARD? | **No** — it is sales/spend. Would need a *new* claim type (e.g. `CONTRACT_SALES` / `SPEND_ACTIVITY`) and future scoring work. |
| 25–100 row pilot? | **Yes** trivially (`$limit=100` via SODA), but as the wrong grain for this pilot. |

**Verdict:** Texas's award-level data (LBB) is comparable to FACTS but documents **no export**;
Texas's API-accessible data (Socrata coop sales) is **payment/sales-level**, not `CONTRACT_AWARD`.
Texas does not offer a confirmed automated *award* path today.

---

## 5. Florida vs Texas Comparison Table

| Dimension | **FL FACTS** | **TX LBB** (award) | **TX Socrata coop sales** (API) |
|---|---|---|---|
| Data grain | Award / contract / PO | Award / contract | Vendor sales / payment |
| Vendor/company name | Yes | Yes | Yes |
| Amount | Yes | Yes | Yes (`purchase_amount`) |
| Date | Begin/end (award) | Award date | order/shipped date |
| Agency | Yes | Yes | `customer_name` |
| Stable record ID | FLAIR Contract ID | Contract ID | `sales_fact_number` |
| Public permalink | **Confirmed** (`ContractDetail.aspx`) | Likely (unverified) | Dataset/row URL |
| Export mechanism | "Download Results" → Excel (manual) | **None documented** | **Full SODA API + CSV** |
| API | None | None | **Yes (Socrata)** |
| Date filter | Yes | Sort/search by award date | Yes (query) |
| Login required | No | No | No |
| Maps to `CONTRACT_AWARD` | **Yes** | **Yes** | **No** (spend → future claim) |
| Automatable without scraping | Unverified (doubtful) | Unverified | **Yes** |
| 25–100 row pilot | Yes (manual) | Possible (export unverified) | Yes (wrong grain) |

The combination Porter actually needs — **award-level + automatable** — is **not confirmed on
either side today**. FACTS wins the *pilot* on award-evidence completeness + confirmed permalink
+ documented manual export.

---

## 6. Final Pilot Recommendation

**Pilot source: Florida FACTS.**
It is the strongest *true-award* source with all required fields, a confirmed public permalink, a
stable FLAIR Contract ID, and a documented manual export sufficient for a 25–100 row read-only
pilot — and it maps to `CONTRACT_AWARD` with no scoring/gate/suppression change.

**Texas role:** backup. Keep **TX LBB** as the alternate award source *if* it turns out to expose
an export/permalink (verify before any production decision). Treat **TX Socrata coop sales** as a
*separate future option* for a different claim type (spend/A-R activity), **not** part of this
`CONTRACT_AWARD` pilot.

**Build remains blocked** (see §1 and §12): the pilot is a manual mapping exercise; the automated
connector is not approved until FACTS export automation is confirmed feasible without scraping, or
a scheduled manual export is explicitly accepted.

---

## 7. Proposed Connector Name

`app/pipeline/connectors/fl_facts.py` (Florida FACTS state-procurement pilot).
Generalize to a shared `state_procurement` connector only after a second state is added.
**Not built in this phase.**

---

## 8. Proposed Small Pilot Scope

**In scope (manual, read-only — no code yet):**
1. Run one date-windowed FACTS Advanced Search; export ≤100 result rows to a scratch Excel/CSV (not the DB).
2. Capture exact export column headers; map them to the existing `extracted_fields` shape.
3. Confirm `ContractDetail.aspx` permalink resolves publicly for a sample of rows.
4. Probe whether the export can be retrieved by a stable/parametrized URL (decides automation feasibility) — **without** emulating the form or scraping. If it can't, record "manual export only."
5. Quantify hard-ID coverage (UEI/FEID vs name+state) to estimate `duplicate_review` load.

**Explicitly out of scope:** any connector code; scraping/bulk harvest; API key use; SAM.gov calls;
scoring/gate/suppression/Salesforce/schema changes; full-pipeline runs.

---

## 9. Proposed Evidence Mapping

(Reuses the USASpending `CONTRACT_AWARD` path — no schema or scoring change.)

| Table | FACTS → Porter mapping |
|---|---|
| `raw_source_events` | One row per exported FACTS contract/PO row. `payload` = raw row JSON; `company_name_raw` = vendor name; `source_record_id` = FLAIR Contract ID (see §10); `source_url` = `ContractDetail.aspx?AgencyId=<id>&ContractId=<flair_id>`; `content_hash` = sha256 of normalized row. |
| `evidence_items` | `claim_supported='CONTRACT_AWARD'`; `extracted_fields` = {company_name, award_amount, action_date=begin date, state_code='FL', commodity/category, awarding_agency, permalink}; `source_url` = permalink; `confidence_score` ≈ 0.85 (manual export, below the 0.9 API value); `freshness_score` from begin date. |
| `companies` | Resolve/create; `state='FL'`; `external_id` computed once (UEI→domain→name+state; usually name+state here). |
| `company_identifiers` | FLAIR Contract ID is a **contract** id — keep it on the raw event/evidence, **not** here. A vendor **FEID**, if present, may be stored as `id_type='feid'` **for reference only — NOT auto-merge** (not in the UEI/domain/state_entity_id allowlist). |
| `signals` | `signal_type='CONTRACT_AWARD'`; `signal_date`=begin date; `award_amount`; `freshness_score`; **`evidence_id` required**. |
| `lead_candidates` | Created/updated via the existing resolution → gate → score path, unchanged. |

---

## 10. Proposed Raw Event Identity Strategy

- **`source_record_id`:** the **FLAIR Contract ID**, type-prefixed to avoid collisions across record
  kinds in the same source — `contract:<flair_id>`, `po:<po_number>`, `grant:<grant_award_id>`.
  This gives a stable, human-traceable handle per FACTS record.
- **Dedup key:** the existing `uq_raw_event_dedup (source_id, content_hash)` constraint. `content_hash`
  = sha256 of the normalized exported row. Identical re-exports dedup automatically.
- **Amendment behavior (honest caveat):** FACTS contracts can be amended (amount/dates change). A
  changed field yields a new `content_hash` → a new `raw_source_event` → potentially a second
  `CONTRACT_AWARD` signal for the *same* contract. Mitigation to decide in the pilot: dedup signals by
  `source_record_id` (FLAIR Contract ID) and only emit a new signal on a material change, rather than
  on any byte difference. Do not over-engineer before the pilot confirms how often this happens.

---

## 11. Risks and Mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| FACTS export not automatable without scraping | **High** | Pilot step 4 probes for a parametrized URL; fallback = scheduled **manual** export feeding the connector (no scraping). Keep build blocked until decided. |
| Picking Texas Socrata by mistake (wrong grain) | Medium | Documented here: coop-sales is spend, not award; reserve it for a separate future claim type only. |
| TX LBB export/permalink unconfirmed | Medium | Verify LBB export before treating it as a real backup; otherwise FL is the only award path. |
| "Begin date" ≠ true award/execution date | Medium | Confirm exact date column in the export header during the pilot. |
| Low hard-ID coverage → `duplicate_review` load | High | Expected; resolve on name+state; quantify in pilot step 5. |
| Contract amendments create duplicate signals | Medium | Dedup signals by FLAIR Contract ID; emit new signal only on material change (§10). |
| Commodity code ≠ NAICS | Low–Med | Store commodity in `extracted_fields`; populate `companies.naics_code` only with a clean crosswalk. |
| Scope creep into new-claim / scoring work | Medium | Hard rule: reuse `CONTRACT_AWARD`; any spend/new claim is a separate later phase. |

---

## 12. Final Go / No-Go

- **Pilot (manual, 25–100 rows): GO — Florida FACTS.** True award evidence, all required fields,
  confirmed public permalink, stable FLAIR Contract ID, documented manual export, clean
  `CONTRACT_AWARD` mapping with no scoring/gate/suppression change.
- **Automated connector build: NO-GO (still blocked).** FACTS has no API and its export
  automatability is unverified; automating it could require scraping, which is prohibited. Unblock
  only when either (a) a parametrized/automatable FACTS export is confirmed, or (b) Porter accepts a
  scheduled **manual** export feeding the connector.
- **Texas:** backup only. LBB is a viable alternate award source *pending* export verification; the
  Socrata coop-sales dataset is **not** a `CONTRACT_AWARD` source and is out of scope for this pilot.

**Honest bottom line:** neither source offers a confirmed *automated + award-level* path today.
Florida is the right **pilot**; the **production connector decision waits** on the FACTS export
automation question.

No SAM.gov calls, no Contactability-Lite apply, no full-pipeline run, no scraping/harvesting, and no
code/schema changes were made to produce this document.
