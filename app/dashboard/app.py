"""
Porter Capital — Lead Intelligence Dashboard.
All business logic lives in app/dashboard/review.py.

Run locally:  streamlit run app/dashboard/app.py

Auth model: reviewer identity is read from the X-Forwarded-Email header injected
by Caddy after OAuth authentication. The user is never asked to type their email.
If the header is absent (local dev without Caddy), review actions are disabled.
"""

import pathlib
import sys

# Streamlit adds the script's directory (app/dashboard/) to sys.path, which makes
# the file "app.py" shadow the "app" package. Insert the project root first so
# Python resolves "app" as the package directory, not this file.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

import uuid

import streamlit as st

from app.dashboard.review import (
    VALID_ACTIONS,
    create_review_decision,
    format_currency,
    format_date,
    get_award_aggregation,
    get_award_gate_summary,
    get_contactability,
    get_latest_run_context,
    get_lead_detail,
    get_reviewer_id,
    get_source_list,
    list_leads_filtered,
)
from app.db.session import SessionLocal
from app.ops.sentry import init_sentry

st.set_page_config(
    page_title="Porter Capital — Lead Intelligence",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)


@st.cache_resource
def _init_sentry_once() -> None:
    init_sentry("dashboard")


_init_sentry_once()

# ── Auth ───────────────────────────────────────────────────────────────────────
# st.context.headers is a dict-like provided by Streamlit; we pass it directly.
# Never trust a form field for reviewer identity.
reviewer_id = get_reviewer_id(dict(st.context.headers))

if not reviewer_id:
    st.sidebar.warning(
        "⚠️ No X-Forwarded-Email header detected. "
        "Review actions are disabled. Run behind Caddy for full auth."
    )
else:
    st.sidebar.success(f"Signed in: {reviewer_id}")

# ── Navigation ─────────────────────────────────────────────────────────────────
page = st.sidebar.radio("Navigation", ["Lead List", "Lead Detail"])

st.title("Porter Capital — Lead Intelligence")
st.warning(
    "Phase 2A research-ready leads only. Not sales-ready. "
    "No verified contacts. No Salesforce push. Human review required."
)

# ── Lead List ──────────────────────────────────────────────────────────────────
if page == "Lead List":

    # ── Fetch run context and source list ─────────────────────────────────────
    with SessionLocal() as ctx_db:
        run_ctx = get_latest_run_context(ctx_db)
        source_list = get_source_list(ctx_db)

    # ── Pipeline Run Context header ───────────────────────────────────────────
    with st.expander("Latest Pipeline Run", expanded=True):
        if not run_ctx["has_runs"]:
            st.info("No pipeline runs recorded yet.")
        else:
            st.caption(
                "**Run date** = when Porter processed the record. "
                "**Evidence date** = when the award/event happened. "
                "**First seen** = when the lead first entered Porter's database. "
                "**Last updated** = when the lead received new evidence or changed status."
            )
            hc1, hc2, hc3, hc4 = st.columns(4)
            hc1.metric("Run ID (short)", str(run_ctx["pipeline_run_id"])[:8] + "…")
            hc2.metric("Status", run_ctx["status"] or "—")
            hc3.metric(
                "Started",
                format_date(run_ctx["started_at"]) if run_ctx["started_at"] else "—",
            )
            hc4.metric(
                "Finished",
                format_date(run_ctx["finished_at"]) if run_ctx["finished_at"] else "running",
            )
            hc5, hc6, hc7, hc8 = st.columns(4)
            hc5.metric("Records Fetched", run_ctx["records_fetched"] or 0)
            hc6.metric("Records Valid", run_ctx["raw_events_stored"] or 0)
            hc7.metric(
                "Leads Scored",
                run_ctx["leads_scored"] if run_ctx["leads_scored"] is not None else "—",
            )
            hc8.metric("Sources Failed", len(run_ctx["errors"]))
            if run_ctx["errors"]:
                st.warning(
                    "Source errors this run: "
                    + "; ".join(e.get("error_text", "unknown") or "unknown" for e in run_ctx["errors"])
                )
            st.caption(
                "Evidence items created / companies resolved / signals created are not "
                "tracked in pipeline_runs — use orchestrator logs for those counts."
            )

    st.divider()

    # ── Sidebar: View selector ────────────────────────────────────────────────
    VIEW_LABELS = {
        "All Active Leads": "all",
        "Latest Run Results (approx.)": "latest_run",
        "Today's New Leads": "today",
        "Recently Updated (7 days)": "recently_updated",
        "Custom Date Range": "custom",
    }
    selected_view_label = st.sidebar.radio("View", list(VIEW_LABELS.keys()))
    view = VIEW_LABELS[selected_view_label]

    date_from = None
    date_to = None
    if view == "custom":
        date_from = st.sidebar.date_input("From date")
        date_to = st.sidebar.date_input("To date")

    if view == "latest_run" and not run_ctx["has_runs"]:
        st.sidebar.caption(
            "No pipeline runs found — showing all active leads instead."
        )

    # ── Sidebar: Filters ──────────────────────────────────────────────────────
    st.sidebar.markdown("**Filters**")

    source_options = {"All sources": None}
    for s in source_list:
        source_options[s["name"]] = s["id"]
    selected_source_label = st.sidebar.selectbox("Source", list(source_options.keys()))
    source_filter_id = source_options[selected_source_label]

    tier_options = ["All", "hot", "warm", "cold", "archive"]
    selected_tier = st.sidebar.selectbox("Tier", tier_options)
    tier_filter = None if selected_tier == "All" else selected_tier

    status_options = ["All", "research", "approved", "rejected", "needs_research", "archived"]
    selected_status = st.sidebar.selectbox("Review Status", status_options)
    status_filter = None if selected_status == "All" else selected_status

    has_contact_filter = st.sidebar.checkbox("Has contactability record")
    has_url_filter = st.sidebar.checkbox("Has evidence URL")

    # ── Sidebar: Sort ─────────────────────────────────────────────────────────
    st.sidebar.markdown("**Sort by**")
    SORT_LABELS = {
        "Highest score": "score_desc",
        "Newest first": "newest_first",
        "Latest updated": "latest_updated",
        "Latest evidence date": "latest_evidence",
        "Largest award amount": "largest_award",
    }
    selected_sort_label = st.sidebar.radio("Sort", list(SORT_LABELS.keys()))
    sort_by = SORT_LABELS[selected_sort_label]

    # ── Query ─────────────────────────────────────────────────────────────────
    latest_run_started_at = run_ctx["started_at"] if run_ctx["has_runs"] else None

    with SessionLocal() as db:
        lead_rows = list_leads_filtered(
            db,
            view=view,
            tier=tier_filter,
            source_id=source_filter_id,
            sales_status=status_filter,
            has_contactability=True if has_contact_filter else None,
            has_evidence_url=True if has_url_filter else None,
            date_from=date_from,
            date_to=date_to,
            sort_by=sort_by,
            latest_run_started_at=latest_run_started_at,
        )

    # ── View label with approximation notice ─────────────────────────────────
    view_title = selected_view_label
    if view == "latest_run":
        view_title += (
            " — approximate: uses updated_at ≥ run.started_at as proxy "
            "(lead_candidates has no first_seen_pipeline_run_id or "
            "last_touched_pipeline_run_id; see schema gap note below)"
        )

    st.subheader(view_title)

    # Summary metrics — derived from already-loaded rows
    total = len(lead_rows)
    hot_count = sum(1 for r in lead_rows if r["lead"].tier == "hot")
    warm_count = sum(1 for r in lead_rows if r["lead"].tier == "warm")
    cold_count = sum(1 for r in lead_rows if r["lead"].tier == "cold")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Leads Shown", total)
    m2.metric("Hot", hot_count)
    m3.metric("Warm", warm_count)
    m4.metric("Cold", cold_count)

    if not lead_rows:
        st.info("No active leads found for the selected view and filters.")
    else:
        st.info("Click a Lead ID cell to copy it, then paste into Lead Detail.")
        table_rows = [
            {
                "Company": (
                    r["company"].canonical_name
                    if r["company"]
                    else str(r["lead"].company_id)
                ),
                "Tier": r["lead"].tier or "—",
                "Score": (
                    str(r["lead"].current_score)
                    if r["lead"].current_score is not None
                    else "—"
                ),
                "Primary Source": r["primary_source"] or "—",
                # "First seen" = when the lead first entered Porter's database (run date)
                "First Seen (run date)": format_date(r["lead"].created_at),
                # "Last updated" = when the lead received new evidence or changed status
                "Last Updated (run date)": format_date(r["lead"].updated_at),
                # "Evidence date" = when the award/event happened
                "Latest Evidence Date": format_date(r["latest_signal_date"]),
                "Updated in Run Window?": "Yes (approx.)" if r["is_new_in_run"] else "No",
                "Review Status": r["lead"].sales_status or "—",
                "Lead ID": str(r["lead"].id),
            }
            for r in lead_rows
        ]
        st.dataframe(table_rows, hide_index=True)

    if view == "latest_run":
        st.caption(
            "**Schema gap:** `lead_candidates` has no `first_seen_pipeline_run_id` or "
            "`last_touched_pipeline_run_id` column, so 'Latest Run Results' cannot be exact. "
            "It uses `updated_at ≥ run.started_at` as a proxy — a lead that received a review "
            "decision during the run window also appears here. "
            "To fix: add `first_seen_pipeline_run_id UUID REFERENCES pipeline_runs(id)` "
            "to `lead_candidates` via a new Alembic migration (not yet applied)."
        )

# ── Lead Detail ────────────────────────────────────────────────────────────────
elif page == "Lead Detail":
    lead_id_str = st.text_input("Paste Lead ID (UUID) from the Lead List")

    if lead_id_str:
        try:
            lead_id = uuid.UUID(lead_id_str.strip())
        except ValueError:
            st.error("Invalid UUID format — copy the Lead ID from the Lead List.")
            st.stop()

        with SessionLocal() as db:
            detail = get_lead_detail(lead_id, db)

        if detail is None:
            st.error("Lead not found.")
        else:
            lead = detail["lead"]
            company = detail["company"]
            latest_score = detail["latest_score"]

            company_name = company.canonical_name if company else "Unknown"
            st.subheader(company_name)
            st.info("Research-ready lead — needs verification before outreach.")

            with st.expander("Company Summary", expanded=True):
                if company:
                    c1, c2 = st.columns(2)
                    c1.write(f"**State:** {company.state or 'Not available'}")
                    c1.write(f"**NAICS:** {company.naics_code or 'Not available'}")
                    c1.write(f"**Business Type:** {company.business_type or 'Not available'}")
                    c2.write(f"**Domain:** {company.website_domain or 'Not available'}")
                    c2.write(f"**Industry:** {company.industry or 'Not available'}")
                    c2.write(f"**Gate Result:** {lead.gate_result or 'Not available'}")

            with st.expander("Score / Tier Summary", expanded=True):
                if latest_score:
                    m1, m2, m3 = st.columns(3)
                    m1.metric("Total Score", latest_score.total_score)
                    m2.metric("Tier", latest_score.tier)
                    m3.metric("Gate", latest_score.gate_result)
                    with st.expander("Score Breakdown Details"):
                        st.json(latest_score.component_breakdown)
                else:
                    st.info("No score computed yet.")

            with st.expander(f"Award Evidence ({len(detail['evidence'])} items)"):
                for ev in detail["evidence"]:
                    fields = ev.extracted_fields or {}
                    action_type = fields.get("action_type")
                    action_desc = fields.get("action_type_description")
                    action_str = ""
                    if action_type:
                        action_str = f" | action_type: {action_type}"
                        if action_desc:
                            action_str += f" ({action_desc})"
                    st.write(
                        f"- [{ev.claim_supported}]({ev.source_url}) "
                        f"— confidence: {float(ev.confidence_score):.2f}{action_str}"
                    )

            with st.expander(f"Signals ({len(detail['signals'])} items)"):
                for sig in detail["signals"]:
                    award = (
                        f" ({format_currency(sig.award_amount)})"
                        if sig.award_amount
                        else ""
                    )
                    st.write(
                        f"- **{sig.signal_type}** ({sig.signal_date}){award} "
                        f"— {sig.signal_strength}"
                    )

            if company:
                with SessionLocal() as agg_db:
                    agg = get_award_aggregation(company.id, agg_db)
                    gate_summary = get_award_gate_summary(company.id, agg_db)

                with st.expander(
                    f"Gate 10 Award Summary ({agg['award_count']} transactions)",
                    expanded=True,
                ):
                    st.markdown("**Gate 10 Award Summary** (signals, positive awards only)")
                    g1, g2, g3, g4, g5 = st.columns(5)
                    g1.metric("Largest Single", format_currency(gate_summary["largest_single"]))
                    g2.metric("90-Day Total", format_currency(gate_summary["recent_total_90d"]))
                    g3.metric("Positive Awards", gate_summary["positive_count"])
                    g4.metric("Most Recent", format_date(gate_summary["most_recent_date"]))
                    g5.metric("Pass Type", gate_summary["pass_type"])
                    st.divider()

                    if agg["award_count"] == 0:
                        st.info("No award amounts found in evidence.")
                    else:
                        m1, m2, m3 = st.columns(3)
                        m1.metric("Total Awarded", format_currency(agg["total_amount"]))
                        m2.metric("Transactions", agg["award_count"])
                        m3.metric("Avg Award Size", format_currency(agg["avg_amount"]))

                        if agg["by_year"]:
                            st.markdown("**By Year**")
                            year_cols = st.columns([1, 1, 2])
                            year_cols[0].markdown("*Year*")
                            year_cols[1].markdown("*Count*")
                            year_cols[2].markdown("*Total*")
                            for row in agg["by_year"]:
                                c = st.columns([1, 1, 2])
                                c[0].write(str(row["year"]))
                                c[1].write(str(row["count"]))
                                c[2].write(format_currency(row["total"]))

                        if agg["by_agency"]:
                            st.markdown("**Top Awarding Agencies**")
                            for row in agg["by_agency"]:
                                st.write(
                                    f"- {row['agency']}: "
                                    f"{row['count']} award(s), "
                                    f"{format_currency(row['total'])}"
                                )

                        if agg["by_action_type"]:
                            st.markdown("**By Action Type**")
                            for row in agg["by_action_type"]:
                                st.write(
                                    f"- {row['action_type']}: "
                                    f"{row['count']} award(s), "
                                    f"{format_currency(row['total'])}"
                                )

                with SessionLocal() as cc_db:
                    contact = get_contactability(company.id, cc_db)

                    with st.expander("Contactability / SAM Entity Validation", expanded=True):
                        st.caption(
                            "SAM entity validation only — confirms the entity exists in "
                            "SAM.gov. It does not confirm a decision-maker email or phone."
                        )
                        if contact is None:
                            st.info(
                                "Not yet enriched — no contactability record for this company."
                            )
                        else:
                            status = contact.contactability_status
                            status_label = (
                                "Needs paid enrichment"
                                if status == "needs_paid_enrichment"
                                else (status or "Not available").replace("_", " ").capitalize()
                            )
                            sam_label = (
                                "Entity matched in SAM"
                                if contact.sam_match_status == "matched"
                                else (contact.sam_match_status or "Not available")
                            )
                            score = contact.contactability_score
                            cc1, cc2 = st.columns(2)
                            cc1.write(f"**Contactability status:** {status_label}")
                            cc1.write(
                                f"**Contactability score:** "
                                f"{score if score is not None else 'Not available'}"
                            )
                            cc1.write(f"**SAM match:** {sam_label}")
                            cc2.write(
                                f"**SAM registration status:** "
                                f"{contact.sam_registration_status or 'Not available'}"
                            )
                            cc2.write(f"**SAM UEI:** {contact.sam_uei or 'Not available'}")
                            cc2.write(
                                f"**Last checked:** {format_date(contact.last_checked_at)}"
                            )
                            if contact.contactability_notes:
                                st.write(f"**Notes:** {contact.contactability_notes}")

            with st.expander(
                f"Review History ({len(detail['review_history'])} decisions)"
            ):
                for dec in detail["review_history"]:
                    st.write(
                        f"- **{dec.action}** by `{dec.reviewer_id}` "
                        f"at {dec.decided_at}"
                    )
                    if dec.note:
                        st.write(f"  > {dec.note}")

            # ── Review Actions ─────────────────────────────────────────────────
            st.divider()
            with st.container():
                st.subheader("Record a Decision")
                if not reviewer_id:
                    st.warning(
                        "Review actions are disabled — no authenticated identity. "
                        "Run behind Caddy so X-Forwarded-Email is present."
                    )
                else:
                    st.info(
                        "This records a human decision only. "
                        "No automatic outreach, Salesforce update, or lead status change will occur."
                    )
                    action = st.selectbox("Action", sorted(VALID_ACTIONS))
                    note_text = st.text_area("Note (optional)")

                    if st.button("Submit Decision"):
                        with SessionLocal() as db:
                            create_review_decision(
                                lead_candidate_id=lead_id,
                                action=action,
                                note=note_text or None,
                                reviewer_id=reviewer_id,
                                db=db,
                            )
                        st.success(f"Decision '{action}' recorded.")
                        st.rerun()
