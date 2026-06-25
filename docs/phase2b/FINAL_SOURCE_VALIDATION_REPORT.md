# Porter Capital — FINAL Source Validation Report (Phase 2B)

**Status:** Definitive. Supersedes the two earlier drafts (Claude's initial validation and the Claude-vs-Codex reconciliation). Where those disagreed, this report states the resolved verdict; where a fact was verified against a primary source, it is marked **[VERIFIED]**.
**Scope:** Evaluation and planning only — no code, connectors, scraping, or paid API calls.
**Wording discipline:** Outputs are **research-ready / internal-review-ready** only, never "sales-ready." A source can identify a company or signal; it does not prove factoring need, contactability, credit quality, or ROI.
**Business exclusion:** All trucking/freight sources are rejected for Porter Capital (handled by the Freight sister company).

---

## 1. Executive Summary

Porter already runs a working pipeline on **USASpending** federal contract awards (`raw_source_events → evidence_items → companies → company_identifiers → signals → lead_candidates → gates → scoring → suppression → Salesforce`), with `CONTRACT_AWARD` as the only proven signal. The question for Phase 2B is which *next* sources are real, useful, accessible, and safe.

Resolved conclusions:

- **Highest-value new capability: State UCC filings** (via a paid aggregator such as Cobalt). A competing UCC-1 over accounts receivable can legally block Porter from purchasing those receivables; a clean position on a qualified lead is a green light. There is **no free official UCC API**, Florida and Tennessee file at the **county level**, and a false "clean" is a real liability — so this is a paid, A/B-tier-only, reviewer-facing capability, not an automated gate.
- **Best first *automated* build: State WARN notices** (download states). Free, public, fast to pilot, lowest legal risk, and a genuine distress/timing signal — though bidirectional (opportunity vs failing company), so it needs an underwriting rule before it scores.
- **Cheap immediate win: OFAC SDN** suppression (free, daily). It is a compliance control, not a generator, and touches suppression — so implement as a reviewer flag under a governed change.
- **Breadth (paid): OpenCorporates / OpenSOSData** for new-registration discovery — weak "why-now," paid, useful only with corroboration.
- **Enrichment only (downstream of a signal): Apollo, Prospeo, Twilio, SAM.gov POC, Creditsafe.** These make a qualified lead contactable/underwritable; they never generate leads.
- **Risk/suppression only: OFAC, OpenSanctions, CourtListener/PACER, Creditsafe (credit view).**

Two earlier claims were corrected after primary-source verification:

- **[VERIFIED] FPDS is rejected** — FPDS.gov ezSearch was decommissioned **Feb 24, 2026**; the ATOM Feed is being retired in 2026; data moved to SAM.gov with a new **SAM.gov Contract Awards API (open.gsa.gov)**. Do not build an FPDS connector.
- **[VERIFIED] OpenCorporates is paid for commercial use** — self-serve API from **£2,250/year** (£225/mo, 500 calls/mo); free access only for journalism/NGO/academia. No commercial "free tier."

Rejected outright: FMCSA, trucking RSS, Ansonia (trucking → Freight); FPDS-as-new-build; PACER automation; BLS/Census/SBA as lead sources; D&B for Phase 2B.

---

## 2. Current System Context
Stack: Python · PostgreSQL · Prefect · Streamlit · Docker · source connectors. CRM: Salesforce. Proven signal: `CONTRACT_AWARD` (USASpending, UEI dedup). **No new database tables are required** for any source below — the work is new signal enums and deferred scoring/gate decisions. The live schema/enum names were never confirmed against the repo; treat all schema mapping as design-intent until verified.

## 3. Evaluation Method
Each source was assessed for: reality & relevance, lead vs reference data, access method (and whether scraping is required/acceptable), freshness, structure, identity fields, factoring-relevant signal, recurring capability, volume, cost, and technical/compliance/data-quality risk. The two highest-impact contested facts (FPDS status, OpenCorporates access) were verified against primary sources. All other vendor pricing, coverage, and rate-limit figures come from the research document and remain **unverified — re-check at build time.** Lead-volume figures are estimates, not measured.

---

## 4. Individual Source Validations

Format per source: **category · decision · phase · access (scraping?) · cost · signal/claim · key fields · dedup keys · freshness · fit · top risks · pilot & acceptance · status label.** Status labels: Confirmed / Likely / Unclear / Risky / Not recommended.

### 4.1 USASpending — Federal Contract Awards
Contract awards · **keep (anchor)** · live · official REST API, no auth (no scraping) · free · signal: won contract → `CONTRACT_AWARD` · fields: name, POP state, NAICS, award amount, action date, Award ID, UEI, award permalink · dedup: UEI, Award ID · daily · fit: discovery+signal+identity+prioritization · risks: federal-only, POP≠HQ, subaward gaps · pilot: ongoing benchmark/quality yardstick · **Confirmed (production).**

### 4.2 SAM.gov Entity API
Contractor registry · **enrichment only** · 3 · official API, free key, ~10/min·10k/day, permission tiers (no scraping) · free-limited · signal: weak/none (`BUSINESS_REGISTRATION` or enrichment) · fields: name, address, UEI, SBA flags, POC · dedup: UEI (strengthens federal dedup) · daily/weekly · fit: identity+enrichment · risks: rate limits, POC≠finance, CUI permissions · pilot: enrich 100 USASpending leads, measure match rate + useful fields · **Likely (enrichment).**

### 4.3 FMCSA / DOT trucking
Permit/license (trucking) · **reject** · — · free API+portals · trucking → Freight sister company · **Not recommended (policy).**

### 4.4 DOL / BLS APIs
Macro stats · **reject as lead source** · — · official API, free · aggregate only, no company rows · fit: territory analytics only · **Not recommended (as leads).**

### 4.5 SBA data (PPP/EIDL/7(a), DSBS)
Gov dataset · **reject (seed-only)** · — · bulk CSV/web, free · historical (2020–22), weak signal · fields: name, address, NAICS, historical loan amount/date · dedup: name+address (weak) · stale · risks: not current demand, weak dedup/evidence · **Risky / seed-only.**

### 4.6 OFAC SDN
Risk/sanctions · **use now (suppression)** · 2A · official bulk download, free, daily (no scraping) · signal: none (negative) · fields: name, aliases, address, program, SDN ID · match: fuzzy name (RapidFuzz ≥85) + state/industry corroboration, never auto-reject on fuzzy alone · fit: risk/compliance · risks: false-positive matches · pilot: match 100 leads, acceptable FP rate · **Confirmed.** Note: touches suppression → reviewer flag under approved change.

### 4.7 OpenSanctions
Risk/sanctions · **defer (suppression)** · later · free/open + paid API, bulk · signal: none · fields: entity name, aliases, address, OpenSanctions ID · daily · fit: risk/compliance · risks: redundant with OFAC for US-only, licensing/PEP overkill · **Likely (redundant with OFAC).**

### 4.8 CourtListener / RECAP
Court/distress · **pilot first (risk use)** · 2B/3 · free API (~5k/day; 40k w/ account); PACER fetch adds cost/legal (no scraping) · signal: lien/bankruptcy/litigation → `LIEN_OR_COURT_SIGNAL` (bidirectional) · fields: party name, court, nature of suit, docket date, docket ID, permalink · dedup: docket ID + party name (weak) · daily/irregular · fit: risk/compliance, partial signal · risks: name ambiguity, direction, legal sensitivity · pilot: 100 known leads, classify opportunity vs suppression, measure precision · **Likely / pilot-gated.**

### 4.9 PACER
Court · **reject (use CourtListener)** · — · paid portal, fees above $30/quarter, automation-restricted · same data as 4.8 which mirrors ~70% free · **Risky.**

### 4.10 FPDS — [VERIFIED Not recommended]
Contract awards · **reject** · — · **deprecated** · FPDS.gov ezSearch decommissioned 2026-02-24; ATOM Feed retiring 2026; replaced by **SAM.gov Contract Awards API (open.gsa.gov)** · same data as USASpending · do not build; evaluate the SAM.gov Contract Awards API if federal coverage expansion is ever wanted · **Not recommended (verified deprecated).**

### 4.11 SEC EDGAR
Disclosure/intent · **needs research** · later · official API, free, descriptive User-Agent, 10 req/s (no scraping) · signal: disclosed factoring/AR-financing intent (new claim) · fields: name, CIK, filing text, date, filing permalink · dedup: CIK · daily · fit: niche intent · risks: public-company ICP mismatch, low volume, boilerplate false positives · pilot: full-text query 6 mo, review 25–50 hits for SMB fit · **Unclear value.**

### 4.12 Census Bureau APIs
Macro stats · **reject as lead source** · — · official API, free · aggregate only, no company names · fit: market sizing only · **Not recommended (as leads).**

### 4.13 State Secretary-of-State registries (50 states)
SOS registry · **pilot first / prefer aggregator** · 2B · **no free official API in any state**; web portals (scraping, some CAPTCHA), a few bulk (FL Sunbiz, NC) · free (high engineering) or paid aggregator · signal: new registration (weak) → `BUSINESS_REGISTRATION` · fields: name, status, type, formation date, agent, state entity ID, address · dedup: state entity ID + name/address (no UEI/EIN; weak cross-state) · daily–monthly · fit: discovery (breadth)+identity · risks: scraping legal/maintenance, CAPTCHA, weak intent, unstable evidence URLs · pilot: 25–100 new registrations in priority states, filter non-trucking NAICS, verify activity · **Risky (prefer aggregator).**

### 4.14 State UCC filings (50 states)
UCC filings · **pilot first → highest-value paid build** · 2B · **no free official API**; web portals or paid vendor; **FL & TN are county-level** (state SOS search returns nothing there) · free-but-fragmented / paid · signal: UCC financing/lien → `UCC_FINANCING` (bidirectional: new filing = borrowing activity; existing A/R lien = qualifier/suppression) · fields: debtor, secured party, filing number, filing date, collateral description (A/R vs all-assets), state · dedup: filing ID + debtor name · daily/weekly · fit: signal+risk+prioritization · risks: **false "clean" = legal liability**, FL/TN county coverage, debtor-name ambiguity, portal fragility · pilot: 25–100 A/B leads through a vendor across a few states incl. one county-level (FL or TN); verify coverage + decision impact · **Likely (value) / Risky (access).** Reviewer-facing first, not an automated gate.

### 4.15 State WARN notices (50 states)
Distress · **build first (after pilot)** · 2A→2B · official CSV/Excel downloads in ~10 states (CA, NY, TX, IL, MA, NJ, OR, PA, WA), HTML/PDF elsewhere (partial scraping) · free · signal: layoff/closure distress → `WARN_DISTRESS_SIGNAL` (bidirectional) · fields: employer, city/county, state, notice date, affected count, layoff type, notice URL · dedup: name+location+date (weak) · weekly–monthly (lumpy) · fit: signal+risk+prioritization · risks: direction ambiguity, weak dedup, format drift, exclude trucking employers · pilot: 25–100 notices CA/TX/NY, exclude trucking, classify closure vs layoff, verify active + ICP; acceptance ≥~30% and direction decided · **Likely / pilot-gated.**

### 4.16 News / RSS (non-trucking: Business Wire, PR Newswire, GlobeNewswire, Google News, StaffingIndustryAnalysts, Manufacturing.net)
News · **use later (supplement)** · 2B · public RSS (no scraping; entity extraction needed) · free · signal: growth/expansion/award news → `NEWS_GROWTH_SIGNAL`/`HIRING_SIGNAL` (low weight) · fields: company (extracted), headline, date, article URL · dedup: name only (weakest) · real-time · fit: discovery breadth + weak signal · risks: high false positives, fragile NER, text-retention terms, public-company bias · pilot: 100 filtered items, manual extraction, measure in-ICP corroborated hit rate · **Risky / pilot-gated.** Exclude trucking feeds (Business Wire Transportation, GlobeNewswire Transportation, Yahoo Finance TRN, FreightWaves, Transport Topics, CCJ).

### 4.17 OpenSOSData
Paid SOS aggregator · **use later** · 2B · paid vendor API (no scraping) · ~$0.03/lookup, ~$94/mo (per doc, unverified) · signal: new registration → `BUSINESS_REGISTRATION` (weak) · fields: name, state, filing date, entity type, agent, status, officers · dedup: state entity ID + name (no UEI/EIN) · daily/weekly · fit: discovery+identity · risks: cost for weak signal, coverage gaps, no contacts · pilot: 100 records in priority states vs direct SOS for completeness · **Unclear (pricing) / Likely (function).**

### 4.18 Cobalt Intelligence
Paid UCC + SOS/TIN vendor · **pilot first → first paid build** · 2B · paid vendor API (no scraping) · ~$0.50/UCC lookup (per doc, unverified), A/B-only · signal: `UCC_FINANCING` + entity verification (bidirectional) · fields: debtor, secured party, filing details, collateral, EIN/TIN · dedup: filing ID + debtor name; EIN strengthens matching · daily/weekly · fit: signal+risk+prioritization · risks: coverage gaps (verify FL/TN county), cost creep, lien interpretation · pilot: 25–100 A/B leads via vendor UI incl. one county-level state · **Likely (value) / Unclear (coverage & pricing).**

### 4.19 Apollo.io
Contact enrichment · **enrichment only** · 3 · paid API (free tier 50 exports/mo) (no scraping) · signal: none (`CONTACT_ENRICHMENT`) · fields: decision-maker name, title, verified email, LinkedIn, direct dial, company domain · dedup: domain + person · vendor-maintained · fit: contact enrichment (A/B-only, downstream of a signal) · risks: outbound/CRM ToU, email accuracy, cost · pilot: 25–100 A/B leads, finance-DM + email find-rate · **Likely (enrichment).** (Connector already available in workspace.)

### 4.20 Prospeo
Email verification · **enrichment only** · 3 · paid API (~$39/mo) · signal: none · fields: deliverability status, MX/SMTP/catch-all/disposable · dedup: email · real-time · fit: deliverability guardrail after Apollo · pilot: verify Apollo batch, bounce <10% · **Likely.**

### 4.21 Creditsafe
Business credit · **use later (risk, legal-gated)** · 3 · paid API OAuth2 (quote) · signal: none (credit/risk) · fields: score, failure probability, payment behavior, judgments, directors, financials · dedup: Creditsafe ID; EIN sometimes · vendor-maintained · fit: risk filtering + prioritization · risks: confirm FCRA/business-credit usage with legal, cost, thin small-private data · pilot: reports on 25 accepted + 25 rejected leads, does score differentiate? · **Likely / legal-gated.**

### 4.22 Twilio Lookup
Phone verification · **enrichment only (optional)** · 3 · paid API (~$0.01/lookup) · signal: none · fields: line type, carrier, line status · dedup: phone (E.164) · real-time · fit: call prioritization · **Confirmed (low priority).**

### 4.23 OpenCorporates — [VERIFIED paid]
Registry aggregator · **use later (paid)** · 2B · official API (no scraping) · **paid: from £2,250/yr (£225/mo, 500 calls/mo); free only for journalism/NGO/academia** · signal: new registration → `BUSINESS_REGISTRATION` (weak) · fields: name, jurisdiction, company number, status, incorporation date, address, officers, **stable `opencorporates_url` permalink** · dedup: opencorporates_url, company number + jurisdiction (no UEI/EIN) · daily/irregular · fit: discovery (breadth)+identity · risks: weak signal, DE/TX coverage thin, paid · pilot: 100 recent registrations TX/FL/GA, field completeness + permalink stability + corroboration · **Confirmed (paid) / Likely (breadth).**

### 4.24 D&B Direct+
Enterprise credit · **reject for 2B (defer P3)** · — · paid enterprise API (~$15k+/yr) · keyed on DUNS (being phased out for UEI); Creditsafe substitutes · **Not recommended now.**

### 4.25 Ansonia Credit Data
Trucking credit · **reject** · — · paid · trucking specialist → Freight · **Not recommended (policy).**

### Out of scope (not in the validated source list)
**People Data Labs** and **Apify Google Maps Scraper** appeared in one prior analysis but were not in the researched source document. Confirm scope with Porter before any work.

---

## 5. Source Ranking Table

| Rank | Source | Category | Decision | Business fit | Access | Cost | Build complexity | Lead quality | Daily volume (est.) | Key risk |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | USASpending | Contract awards | Keep (anchor) | High | Official API | Free | N/A (live) | High | 50–200 | Federal-only |
| 2 | State UCC / Cobalt | UCC filings | Pilot → first paid build | High | Paid vendor | Paid (verify) | Med (vendor) | High (qualifier) | 10–50 checks | False "clean" = liability |
| 3 | State WARN | Distress | Build first (after pilot) | High (timing) | CSV/portal | Free | Low–Med | Med | 10–50 | Direction ambiguity |
| 4 | OFAC SDN | Risk | Use now (suppression) | High (risk) | Bulk download | Free | Low | N/A | 0 leads | False-positive match |
| 5 | OpenCorporates | Registry | Use later (paid) | Medium | API | Paid (£2,250+/yr) | Low–Med | Low–Med | 50–200 | Weak signal; paid |
| 6 | Apollo + Prospeo | Enrichment | Enrichment only | High (contact) | Paid API | Paid | Low–Med | N/A | enrich-bound | ToU/accuracy |
| 7 | Creditsafe | Credit | Risk (legal-gated) | Med–High | Paid API | Paid | Medium | N/A | enrich-bound | FCRA usage |
| 8 | OpenSOSData | Registry | Use later | Medium | Paid API | Paid (~$94/mo) | Low–Med | Low–Med | 50–200 | Cost for weak signal |
| 9 | News/RSS (non-trucking) | News | Use later | Low–Med | RSS | Free | Medium | Low–Med | 10–50 useful | Noisy; weak dedup |
| 10 | CourtListener | Court | Pilot (risk) | Low–Med | API | Free | Medium | Low–Med | 10–50 | Direction; name match |
| 11 | SAM.gov Entity | Registry | Enrichment only | Medium | API | Free-limited | Low–Med | Med (enrich) | enrich-bound | Rate limits |
| 12 | State SOS (direct) | Registry | Pilot / aggregator | Low–Med | Scrape | Free (high eng) | High | Low–Med | 50–200 | Legal/maintenance |
| 13 | Twilio Lookup | Enrichment | Enrichment only | Support | Paid API | Paid (tiny) | Low | N/A | enrich-bound | Marginal value |
| 14 | SEC EDGAR | Intent | Needs research | Niche | API | Free | Low–Med | Low–Med | 0–10 | ICP mismatch |
| 15 | OpenSanctions | Risk | Defer (suppression) | Med | API/bulk | Free/paid | Low–Med | N/A | 0 leads | Redundant w/ OFAC |
| 16 | SBA | Gov dataset | Reject (seed-only) | Low | CSV | Free | Low | Low | ~0 new | Stale |
| 17 | PACER | Court | Reject | Low–Med | Paid | Paid | Med | Low–Med | Unknown | Redundant/paid |
| 18 | BLS/DOL | Macro | Reject (analytics) | Low | API | Free | N/A | Not useful | 0 | No companies |
| 19 | Census | Macro | Reject (analytics) | Low | API | Free | N/A | Not useful | 0 | No companies |
| 20 | D&B | Credit | Reject (defer P3) | Low now | Paid | Paid $$$ | Med | N/A | enrich-bound | Cost; DUNS fading |
| 21 | FPDS | Contract awards | Reject [VERIFIED] | — | Deprecated | — | — | — | — | Decommissioned |
| 22 | FMCSA | Trucking | Reject | N/A | API/scrape | Free | — | N/A | — | Trucking exclusion |
| 23 | Ansonia | Trucking credit | Reject | N/A | Paid | Paid | — | N/A | — | Trucking exclusion |

---

## 6. Recommended Build Order

| Priority | Source | Why now | What's needed first | Build type | Go/No-Go |
|---|---|---|---|---|---|
| P0 | OFAC SDN | Free compliance hygiene | Governance for a suppression change | Daily download + fuzzy match (reviewer flag) | Go (governed) |
| P1 | State WARN (download states) | Cheapest real distress signal | Manual pilot + WARN direction decision | Scheduled CSV/Excel import | Go if acceptance ≥~30% + direction set |
| P2 | State UCC via Cobalt | Highest factoring value | Coverage (FL/TN county) + pricing + ToU; manual pilot | Paid vendor API, A/B-only, reviewer-facing | Go if clean/dirty changes decisions + coverage OK |
| P3 | OpenCorporates / OpenSOSData | Registration breadth | Budget + coverage test | Paid API | Go if registrations corroborate to real leads |
| P4 | Apollo + Prospeo (+Twilio) | Make A/B leads contactable | A real signal layer first; outbound ToU | Paid enrichment | Go (A/B-only) after P1/P2 |
| P5 | Creditsafe | Underwriting/risk | Legal FCRA confirmation + budget | Paid enrichment | Conditional on legal |
| Later | CourtListener, SEC EDGAR, News/RSS, OpenSanctions, SAM.gov | Lower priority / niche | Per-source pilot above | Varies | Pilot first |
| No-Go | FPDS, PACER, SBA, BLS, Census, D&B, FMCSA, Ansonia | Deprecated / analytics / trucking / cost | — | — | No |

**Best source to build first:** WARN notices (free, public, fast pilot, low legal risk) as the first automated build; UCC/Cobalt as the highest-value first *paid* build, in parallel pilot.
**Best later-phase source:** UCC at full automation (after the gate-vs-flag rule is set), then registration breadth and the enrichment layer.

---

## 7. Cross-cutting classifications

**Reject:** FMCSA, trucking RSS, Ansonia (trucking); FPDS [VERIFIED deprecated]; PACER; SBA (seed-only); BLS/DOL & Census (analytics only); D&B (defer P3).
**Need paid budget:** Cobalt, OpenSOSData, OpenCorporates [VERIFIED £2,250+/yr], Apollo, Prospeo, Twilio, Creditsafe, D&B, OpenSanctions (production API). *(All pricing except OpenCorporates is unverified.)*
**Need legal/terms review:** all state SOS/UCC scraping, WARN PDF/HTML parsing, RSS text retention, Apollo outbound/CRM sync, Creditsafe FCRA usage, CourtListener/PACER, all vendor ToU.
**Require new signal logic (deferred):** `UCC_FINANCING`, `WARN_DISTRESS_SIGNAL`, `BUSINESS_REGISTRATION`, `NEWS_GROWTH_SIGNAL`, court `LIEN_OR_COURT_SIGNAL` (several are bidirectional and need an underwriting opportunity-vs-suppression rule).
**Reuse existing CONTRACT_AWARD (no change):** USASpending only. (FPDS would have, but it's rejected.)
**Enrichment only:** Apollo, Prospeo, Twilio, SAM.gov POC, D&B; OpenCorporates/OpenSOSData when not used as a signal.
**Risk/suppression only:** OFAC, OpenSanctions, CourtListener/PACER, Creditsafe (credit view), the existing-lien side of UCC.
**Requires scraping (legal review):** direct State SOS, direct State UCC, HTML/PDF WARN states. (Avoided by using official downloads or licensed vendors.)

---

## 8. Verified Facts vs Unverified Assumptions

**Verified against primary sources:**
- FPDS.gov ezSearch decommissioned **Feb 24, 2026**; ATOM Feed retiring 2026; replaced by SAM.gov Contract Awards API (open.gsa.gov). *(SAM.gov announcement.)*
- OpenCorporates self-serve API from **£2,250/year**; free only for public-benefit. *(opencorporates.com/pricing.)*

**Not verified — check before relying:** Cobalt pricing **and FL/TN county UCC coverage**; OpenSOSData pricing/coverage; Apollo/Prospeo/Twilio/Creditsafe tiers; OpenSanctions production API; the new SAM.gov Contract Awards API schema; all lead-volume estimates; and **the live Porter schema/enum names** (mapping above is design-intent only).

## 9. Open Questions for Porter
1. Should a competing A/R lien (UCC) auto-suppress a lead or only flag it for a reviewer?
2. Is a WARN/bankruptcy event an opportunity (bridge funding) or a disqualifier? (Underwriting must decide before scoring.)
3. Approved budget for paid pilots (Cobalt, OpenCorporates, enrichment)?
4. For non-federal sources lacking UEI/EIN, is name+state+address matching acceptable, or is a hard identifier required before promoting a lead?
5. Are People Data Labs and Apify Google Maps intended to be in scope?

## 10. Final Recommendation
Keep USASpending as the anchor. Add **OFAC suppression** (free, governed) and run the **WARN pilot** now — WARN is the best first automated build. In parallel, pilot **UCC via Cobalt** as the highest-value paid capability (reviewer-facing, A/B-only), pending coverage and pricing verification. Treat **OpenCorporates/OpenSOSData** as paid breadth and **Apollo/Prospeo/Twilio/Creditsafe** as strictly downstream enrichment on qualified leads. Reject trucking sources, **FPDS (verified deprecated)**, PACER, BLS/Census/SBA-as-leads, and D&B. Everything produced remains research-ready, not sales-ready, and no gates/scoring/suppression/Salesforce logic changes without an explicit approved change. Re-verify all vendor pricing/coverage and the live schema before any build.

---

*Final report. Evaluation and planning only — no code, connectors, scraping, or paid API calls. Two facts verified against primary sources (FPDS, OpenCorporates); all other vendor figures unverified and labeled accordingly.*
