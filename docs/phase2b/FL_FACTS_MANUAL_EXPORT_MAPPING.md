# Phase 2B — Florida FACTS Manual Export Mapping

**Status:** Internal research / review-ready. **DRAFT — Part 1 of 2.** Not sales-ready.
**Date:** 2026-06-16
**Author:** Engineering (Phase 2B manual FACTS pilot validation)
**Scope:** Manual export validation only. No connector, no application/schema/scoring/gates/
suppression/Salesforce changes, no full-pipeline run, no SAM.gov calls, no Contactability-Lite
apply, **no scraping, no browser-form automation, no bulk harvesting.**

> **Why this is a draft.** Sections that describe the *actual* exported file — **§3 Export File
> Summary, §4 Column Header Inventory, §5 Required Field Validation, §6 Evidence Link Validation**
> — depend on the file the user will download manually from the official FACTS portal. They are
> marked **PENDING EXPORT FILE** and will be filled from the real file in Part 2. Column names in
> the *proposed* mapping sections (§7–§10) are **expected** names to be confirmed against the real
> headers — they are not asserted as fact. Nothing here was scraped or harvested.

---

## 1. Executive Summary

**The manual export method is available and documented (§2); file-level confirmation is pending
the user's upload.** Florida FACTS exposes award/contract-level records with the fields Porter
needs and a public per-contract permalink, and it supports a small manual Excel/CSV export — which
is exactly the safe, no-scraping path this pilot requires. The open questions that only the real
file can answer are the **exact column headers**, whether a **direct detail-URL column** exists (vs.
needing to *construct* the permalink), and whether every row is genuinely **award-level**.

Current standing (unchanged until the file is inspected):
- **Manual upload pilot: feasible** (this is the recommended path).
- **Automated connector build: still BLOCKED** (no API; form automation is prohibited).

---

## 2. Manual Export Method Used

These are the **exact browser steps** for the user to perform on the official portal. (Claude does
not execute these — no scraping/automation.)

**Portal:** Florida FACTS — `https://facts.fldfs.com/` → **Advanced Search**
(`https://facts.fldfs.com/Search/ContractAdvancedSearch.aspx`).

**Goal:** a small, award-level result set of **25–100 rows**.

1. **Open Advanced Search** at the URL above (no login required for public search/view).
2. **Set filters** to keep the result set small and award-level:
   - **Agency Name:** pick **one** mid-size agency (e.g. a single department) rather than "ALL
     AGENCIES" — this is the most reliable way to cap row count.
   - **Date Range (Beginning dates):** set a **narrow window**, e.g. a single recent month
     (Begin = first of a recent month, End = end of that month). Widen/narrow to land in 25–100 rows.
   - **Dollar Value (From):** optionally set **From = 50000** to bias toward substantive contracts
     (mirrors the kind of contract Porter cares about) and to reduce noise/row count.
   - **Commodity/Service Type:** leave as **All** for the pilot (we want to see the full column set).
3. **Run the search.** If results exceed ~100 rows, **narrow the date window** or **raise the dollar
   floor** until the count is 25–100. (Do not export thousands of rows — this is a sample, not a harvest.)
4. **Export:** on the **Results** page, click **"Download Results"** (exports to Excel/spreadsheet).
   - ⚠️ Do **not** use the "Download"/"Download Crosswalk" buttons on the *search form* — those export
     the commodity lookup and the FLID→UNSPSC crosswalk, **not** your results.
5. **Verify the FLAIR Contract ID:** in the results grid (and again on the detail page) confirm there
   is a **FLAIR Contract ID** value per row. The **Agency-Assigned Contract ID** is typically the
   clickable link to the detail page.
6. **Open one ContractDetail page:** click the **Agency-Assigned Contract ID** link on any row. It
   should open a URL of the form
   `https://facts.fldfs.com/Search/ContractDetail.aspx?AgencyId=<agency_id>&ContractId=<flair_id>`.
   **Copy that full URL** into the chat so we can confirm the permalink pattern against the file's fields.
7. **Save & send:** save the export as **.xlsx or .csv** and upload it here. Also paste the one
   ContractDetail URL from step 6.

**What to send back:** (a) the exported file, (b) one ContractDetail URL, (c) the approximate row count.

---

## 3. Export File Summary

**PENDING EXPORT FILE.** To be filled from the user's upload: file name, format (.xlsx/.csv), row
count, column count, date window/filters actually used, and any obvious header/footer or merged-cell
quirks from the FACTS export.

---

## 4. Column Header Inventory

**PENDING EXPORT FILE.** To be filled: the verbatim list of every column header in the export, in
order, with a one-line note on the data each appears to hold (from inspecting values, not assuming).

---

## 5. Required Field Validation

**PENDING EXPORT FILE.** To be completed against the real headers. Expected mapping targets (status
to be set to ✅ confirmed / ⚠️ partial / ❌ absent once the file is inspected):

| Required field | Expected FACTS column (to confirm) | Status |
|---|---|---|
| Vendor / company name | Vendor Name (or similar) | PENDING |
| Contract amount | Contract Amount / Total Amount | PENDING |
| Begin / execution / award date | Contract Begin Date (or Execution Date) | PENDING |
| Agency | Agency Name | PENDING |
| Contract ID / FLAIR Contract ID | FLAIR Contract ID (+ Agency-Assigned Contract ID) | PENDING |
| Commodity / category | Commodity/Service Type | PENDING |
| Direct detail URL column | (may not exist — see §6) | PENDING |

---

## 6. Evidence Link / Permalink Validation

**PARTIAL — pattern known, file confirmation PENDING.**

- **Known permalink pattern** (from prior validation, to confirm with the user's step-6 URL):
  `https://facts.fldfs.com/Search/ContractDetail.aspx?AgencyId=<agency_id>&ContractId=<flair_id>`.
- **Open question for the file:** does the export include a **ready-made detail-URL column**, or must
  we **construct** the permalink from `AgencyId` + `FLAIR Contract ID`? If `AgencyId` is **not** a
  column in the export, we must confirm how to obtain it (e.g. an agency-name→AgencyId lookup, or it
  is embedded in another field). **This is the single most important thing to verify in the file**,
  because every signal/score needs a citing evidence URL.

---

## 7. Proposed Raw Event Identity Strategy

*(Design proposal — column names to confirm against §4.)*

- **`source_record_id`:** the **FLAIR Contract ID**, type-prefixed to avoid cross-kind collisions —
  `contract:<flair_id>` (and `po:<po_number>` / `grant:<grant_award_id>` if those record kinds appear).
- **Dedup key:** the existing `uq_raw_event_dedup (source_id, content_hash)` constraint;
  `content_hash` = sha256 of the normalized export row. Identical re-exports dedup automatically.
- **Amendment behavior (caveat):** FACTS contracts can be amended (amount/date changes), producing a
  new `content_hash` → a new raw event → potentially a duplicate `CONTRACT_AWARD` signal for the same
  contract. Mitigation to decide after seeing real data: dedup signals by FLAIR Contract ID and emit a
  new signal only on a material change. Do not over-engineer before the file shows how often this occurs.

---

## 8. Proposed Evidence Mapping

*(Design proposal — reuses the USASpending `CONTRACT_AWARD` path; no schema/scoring change. Column
names to confirm against §4.)*

| Table | FACTS → Porter mapping |
|---|---|
| `raw_source_events` | One row per export row. `payload` = raw row JSON; `company_name_raw` = vendor name; `source_record_id` = FLAIR Contract ID (§7); `source_url` = constructed/【confirmed】ContractDetail permalink; `content_hash` = sha256 of normalized row. |
| `evidence_items` | `claim_supported='CONTRACT_AWARD'`; `extracted_fields` = {company_name, award_amount, action_date=begin date, state_code='FL', commodity/category, awarding_agency, permalink}; `source_url` = permalink; `confidence_score` ≈ 0.85 (manual export); `freshness_score` from begin date. |

---

## 9. Proposed Company Resolution Mapping

*(Design proposal.)*

| Table | Mapping |
|---|---|
| `companies` | Resolve/create; `state='FL'`; `canonical_name`/`normalized_name` from vendor name via the existing normalizer; `external_id` computed once (UEI→domain→name+state — expected **name+state** here). |
| `company_identifiers` | FLAIR Contract ID is a **contract** id — keep it on the raw event/evidence, **not** here. A vendor **FEID**, *if present in the export*, may be stored as `id_type='feid'` **for reference only — NOT auto-merge** (not in the UEI/domain/state_entity_id hard-ID allowlist). |

**Expected resolution reality:** state contracts rarely carry UEI/domain → most companies resolve on
**name+state**, which can raise `duplicate_review` rows (by design — founding rule 2 forbids name-only
auto-merge). Quantify from the file in Part 2.

---

## 10. Proposed Signal Mapping

*(Design proposal.)*

| Table | Mapping |
|---|---|
| `signals` | `signal_type='CONTRACT_AWARD'`; `signal_date` = contract begin date; `award_amount` = contract amount; `freshness_score` from begin date; **`evidence_id` required** (the evidence row in §8). |
| `lead_candidates` | Created/updated via the existing resolution → gate → score path, unchanged. The award-amount gate (Gate 10) and `CONTRACT_AWARD` scoring filter apply with no code change. |

---

## 11. Data Quality Risks

*(Initial list; refine against the file.)*

| Risk | Likelihood | Mitigation |
|---|---|---|
| No `AgencyId` column → can't construct permalink | Medium | Verify in §6; if missing, find an agency-name→AgencyId lookup before building |
| "Begin date" ≠ true award/execution date | Medium | Confirm the exact date column in the file; choose the one closest to award/execution |
| Rows are mixed kinds (contracts + POs + grants/amendments) | Medium | Inspect a record-type column; decide which kinds qualify as `CONTRACT_AWARD` |
| Low hard-ID coverage → `duplicate_review` load | High | Expected; resolve on name+state; quantify from file |
| Amounts as text / with symbols / negative or zero | Medium | Normalize at the Pydantic boundary; the existing amount gate ignores non-positive |
| Commodity code ≠ NAICS | Low–Med | Store commodity in `extracted_fields`; populate `naics_code` only with a clean crosswalk |
| Vendor name variants (DBA, punctuation) | Medium | Existing normalizer handles legal suffixes; watch "stays-distinct" cases |

---

## 12. Automation Feasibility

**Doubtful — unchanged from the FL/TX access check.** FACTS has no documented API and the results
export is a **post-search download on an ASP.NET WebForms page**; a stable parametrized GET URL for
the export is unverified. Automating it would likely require emulating the form/session, which is
**scraping — explicitly prohibited here**. → Automated ingestion is **not approved**. The realistic
automation path, if Porter wants recurring data, is a **scheduled human export** (the §2 steps run on a
cadence) feeding a manual upload, not a self-driving connector.

---

## 13. Manual Upload Feasibility

**Feasible and recommended.** A human runs the §2 export (25–100 rows), saves CSV/XLSX, and uploads
it. A future connector could ingest such an uploaded file from a watched directory — no portal access,
no scraping, no API. This is the lowest-risk way to get real FACTS data into the mapping pipeline and
is the natural Phase 2B pilot shape. **File-level confirmation pending the upload.**

---

## 14. Connector Go / No-Go Recommendation

**Current standing (to finalize in Part 2 after file inspection):**
- **Manual upload pilot: GO** — once the file confirms the headers/permalink, the mapping is
  straightforward and reuses `CONTRACT_AWARD`.
- **Automated (portal-driving) connector: NO-GO / BLOCKED** — no API; form automation is prohibited.
- **File-based ingestion connector (reads a manually-uploaded export): conditional GO** — pending the
  file confirming (a) exact headers, (b) a usable permalink (column or constructable), and (c) that
  rows are award-level. These are the §3–§6 PENDING items.

---

## 15. Next Build Recommendation

1. **User performs §2 export** and uploads the CSV/XLSX + one ContractDetail URL.
2. **Claude fills §3–§6** from the real file (no external calls, no scraping) and finalizes §14.
3. **If confirmed:** propose a *file-ingestion* pilot — a small, reviewable module that reads one
   uploaded FACTS export and produces `raw_source_events`/`evidence_items`/`signals` as `CONTRACT_AWARD`,
   gated behind explicit approval (still no portal automation).
4. **Defer** any portal-automation / API approach unless Florida publishes an official API or Porter
   accepts a scheduled manual export cadence.

---

*No SAM.gov calls, no Contactability-Lite apply, no full-pipeline run, no scraping/automation/harvesting,
and no code/schema changes were made to produce this document.*
