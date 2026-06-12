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
    get_lead_detail,
    get_reviewer_id,
    list_reviewable_leads,
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
    tier_options = ["All", "hot", "warm", "cold", "archive"]
    selected_tier = st.sidebar.selectbox("Filter by Tier", tier_options)
    tier_filter = None if selected_tier == "All" else selected_tier

    with SessionLocal() as db:
        leads = list_reviewable_leads(db, tier=tier_filter)

    st.subheader("Active Leads")

    # Summary metrics — computed from already-loaded leads, no extra DB call
    total = len(leads)
    hot_count = sum(1 for lead in leads if lead.tier == "hot")
    warm_count = sum(1 for lead in leads if lead.tier == "warm")
    cold_count = sum(1 for lead in leads if lead.tier == "cold")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Active Leads", total)
    m2.metric("Hot", hot_count)
    m3.metric("Warm", warm_count)
    m4.metric("Cold", cold_count)

    if not leads:
        st.info("No active leads found for this filter.")
    else:
        st.info("Click a Lead ID cell to copy it, then paste into Lead Detail.")
        rows = [
            {
                "Company": (
                    lead.company.canonical_name if lead.company else str(lead.company_id)
                ),
                "Tier": lead.tier or "Not available",
                "Score": (
                    str(lead.current_score)
                    if lead.current_score is not None
                    else "Not available"
                ),
                "Review Status": lead.sales_status or "Not available",
                "Lead ID": str(lead.id),
            }
            for lead in leads
        ]
        st.dataframe(rows, hide_index=True)

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
