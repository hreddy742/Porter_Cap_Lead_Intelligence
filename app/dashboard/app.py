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

# ── Lead List ──────────────────────────────────────────────────────────────────
if page == "Lead List":
    tier_options = ["All", "hot", "warm", "cold", "archive"]
    selected_tier = st.sidebar.selectbox("Filter by Tier", tier_options)
    tier_filter = None if selected_tier == "All" else selected_tier

    with SessionLocal() as db:
        leads = list_reviewable_leads(db, tier=tier_filter)

    st.subheader("Active Leads")

    if not leads:
        st.info("No active leads found for this filter.")
    else:
        header = st.columns([3, 1, 1, 2, 3])
        header[0].markdown("**Company**")
        header[1].markdown("**Tier**")
        header[2].markdown("**Score**")
        header[3].markdown("**Sales Status**")
        header[4].markdown("**Lead ID** (paste into Lead Detail)")
        st.divider()

        for lead in leads:
            company_name = (
                lead.company.canonical_name
                if lead.company
                else str(lead.company_id)
            )
            cols = st.columns([3, 1, 1, 2, 3])
            cols[0].write(company_name)
            cols[1].write(lead.tier or "—")
            cols[2].write(
                str(lead.current_score) if lead.current_score is not None else "—"
            )
            cols[3].write(lead.sales_status)
            cols[4].code(str(lead.id))

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

            with st.expander("Company Details", expanded=True):
                if company:
                    c1, c2 = st.columns(2)
                    c1.write(f"**State:** {company.state or '—'}")
                    c1.write(f"**NAICS:** {company.naics_code or '—'}")
                    c1.write(f"**Business Type:** {company.business_type or '—'}")
                    c2.write(f"**Domain:** {company.website_domain or '—'}")
                    c2.write(f"**Industry:** {company.industry or '—'}")
                    c2.write(f"**Gate Result:** {lead.gate_result or '—'}")

            with st.expander("Latest Score", expanded=True):
                if latest_score:
                    m1, m2, m3 = st.columns(3)
                    m1.metric("Total Score", latest_score.total_score)
                    m2.metric("Tier", latest_score.tier)
                    m3.metric("Gate", latest_score.gate_result)
                    st.json(latest_score.component_breakdown)
                else:
                    st.info("No score computed yet.")

            with st.expander(f"Evidence ({len(detail['evidence'])} items)"):
                for ev in detail["evidence"]:
                    st.write(
                        f"- [{ev.claim_supported}]({ev.source_url}) "
                        f"— confidence: {float(ev.confidence_score):.2f}"
                    )

            with st.expander(f"Signals ({len(detail['signals'])} items)"):
                for sig in detail["signals"]:
                    award = (
                        f" (${float(sig.award_amount):,.0f})"
                        if sig.award_amount
                        else ""
                    )
                    st.write(
                        f"- **{sig.signal_type}** ({sig.signal_date}){award} "
                        f"— {sig.signal_strength}"
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
            st.subheader("Record a Decision")
            if not reviewer_id:
                st.warning(
                    "Review actions are disabled — no authenticated identity. "
                    "Run behind Caddy so X-Forwarded-Email is present."
                )
            else:
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
