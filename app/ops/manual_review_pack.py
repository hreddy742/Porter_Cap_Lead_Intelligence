"""
Manual review pack builder — read-only.

No inserts, updates, deletes, commits, pipeline runs, or external API calls.
"""
from __future__ import annotations

import csv
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from textwrap import dedent

from sqlalchemy import text
from sqlalchemy.orm import Session

NOT_AVAILABLE = "Not available"

CSV_FIELDNAMES = [
    "rank",
    "company_name",
    "tier",
    "current_score",
    "review_status",
    "state",
    "city",
    "naics_code",
    "naics_description",
    "largest_single_award",
    "award_total_90d",
    "lifetime_award_total",
    "positive_award_count",
    "most_recent_award_date",
    "gate10_pass_type",
    "primary_agency",
    "best_evidence_claim",
    "best_evidence_confidence",
    "source_url",
    "why_this_lead_is_included",
    "reviewer_notes",
]

_CAUTION = (
    "These are not sales-ready leads. "
    "Contacts are not verified. "
    "Factoring need is not proven. "
    "Human review is required before outreach."
)

_LIMITATIONS = [
    "Contact information has not been verified.",
    "No Salesforce push performed.",
    "No risk or compliance enrichment.",
    "Factoring need has not been proven.",
    "ROI has not been proven.",
]


# ── Pure helpers ───────────────────────────────────────────────────────────────

def _na(value) -> str:
    """Return str(value) or NOT_AVAILABLE when value is None or empty string."""
    if value is None or value == "":
        return NOT_AVAILABLE
    return str(value)


def _fmt_currency(value) -> str:
    """Return formatted USD string, or NOT_AVAILABLE when value is None."""
    if value is None:
        return NOT_AVAILABLE
    try:
        amount = Decimal(str(value))
        return f"${float(amount):,.0f}"
    except (InvalidOperation, TypeError, ValueError):
        return NOT_AVAILABLE


def _classify_pass_type(
    largest_single: Decimal,
    recent_total_90d: Decimal,
    min_single: Decimal,
    min_90d: Decimal,
) -> str:
    """Mirror Gate 10 classification — display-only, never used for scoring or gating."""
    if largest_single >= min_single:
        return "single_award_pass"
    if recent_total_90d >= min_90d:
        return "aggregate_90d_pass"
    if largest_single > Decimal("0") or recent_total_90d > Decimal("0"):
        return "below_threshold"
    return "unknown"


def _build_why(
    pass_type: str,
    largest: Decimal,
    recent_90d: Decimal,
    positive_count: int,
    most_recent_date,
) -> str:
    """Generate a plain-text reason this lead is included — no AI, no guessing."""
    date_str = str(most_recent_date) if most_recent_date else "unknown date"
    if pass_type == "single_award_pass":
        return (
            f"Award evidence: largest single award {_fmt_currency(largest)}. "
            f"Gate 10: single_award_pass. Most recent award: {date_str}. "
            "Needs verification before outreach."
        )
    if pass_type == "aggregate_90d_pass":
        return (
            f"Award evidence: 90-day total {_fmt_currency(recent_90d)} "
            f"across {positive_count} award(s). "
            f"Gate 10: aggregate_90d_pass. Most recent award: {date_str}. "
            "Needs verification before outreach."
        )
    if pass_type == "below_threshold":
        return (
            f"Award evidence below Gate 10 threshold. "
            f"Largest single: {_fmt_currency(largest)}. "
            "Needs verification before outreach."
        )
    return "No qualifying award evidence found. Needs verification before outreach."


# ── SQL builders ──────────────────────────────────────────────────────────────

def _leads_query_sql(tier: str | None) -> str:
    """Return the SQL string for the active-lead selection query.

    The optional tier filter is injected as a hardcoded clause (not user data).
    The tier *value* is always bound as :tier — never interpolated.
    """
    tier_clause = "AND lc.tier = :tier" if tier else ""
    return dedent(f"""
        SELECT
          lc.id                                                              AS lead_candidate_id,
          lc.company_id,
          lc.tier,
          lc.current_score,
          lc.sales_status,
          c.canonical_name,
          c.city,
          c.state,
          c.naics_code,
          c.naics_description,
          COUNT(s.id) FILTER (WHERE s.award_amount > 0)                    AS positive_count,
          MAX(s.award_amount) FILTER (WHERE s.award_amount > 0)            AS largest_single,
          SUM(s.award_amount) FILTER (
            WHERE s.award_amount > 0
              AND s.signal_date >= CURRENT_DATE - INTERVAL '90 days'
          )                                                                  AS recent_total_90d,
          SUM(s.award_amount) FILTER (WHERE s.award_amount > 0)            AS lifetime_total,
          MAX(s.signal_date) FILTER (WHERE s.award_amount > 0)             AS most_recent_date
        FROM lead_candidates lc
        JOIN companies c ON c.id = lc.company_id
        LEFT JOIN signals s
          ON s.company_id = lc.company_id
         AND s.signal_type = 'CONTRACT_AWARD'
        WHERE lc.status = 'active'
          AND lc.deleted_at IS NULL
          AND c.deleted_at IS NULL
          {tier_clause}
        GROUP BY
          lc.id, lc.company_id, lc.tier, lc.current_score, lc.sales_status,
          c.canonical_name, c.city, c.state, c.naics_code, c.naics_description
        ORDER BY
          CASE lc.tier WHEN 'hot' THEN 1 WHEN 'warm' THEN 2 WHEN 'cold' THEN 3 ELSE 4 END ASC,
          lc.current_score DESC NULLS LAST,
          largest_single DESC NULLS LAST,
          recent_total_90d DESC NULLS LAST,
          most_recent_date DESC NULLS LAST
        LIMIT :limit
    """).strip()


# ── DB query functions ────────────────────────────────────────────────────────

def _fetch_best_evidence(company_id, db: Session) -> dict:
    """Return best CONTRACT_AWARD evidence for a company — deterministic selection.

    Order: highest confidence_score, then most recent action_date in extracted_fields,
    then most recent created_at.  Returns NOT_AVAILABLE for all fields if none found.
    """
    row = db.execute(
        text("""
            SELECT
              ei.source_url,
              ei.confidence_score,
              ei.claim_supported,
              ei.extracted_fields->>'awarding_agency' AS awarding_agency
            FROM evidence_items ei
            WHERE ei.company_id = :cid
              AND ei.claim_supported = 'CONTRACT_AWARD'
            ORDER BY
              ei.confidence_score DESC NULLS LAST,
              (ei.extracted_fields->>'action_date') DESC NULLS LAST,
              ei.created_at DESC NULLS LAST
            LIMIT 1
        """),
        {"cid": str(company_id)},
    ).fetchone()

    if row is None:
        return {
            "source_url": NOT_AVAILABLE,
            "confidence": NOT_AVAILABLE,
            "claim": NOT_AVAILABLE,
            "agency": NOT_AVAILABLE,
        }
    return {
        "source_url": _na(row.source_url),
        "confidence": _na(row.confidence_score),
        "claim": _na(row.claim_supported),
        "agency": _na(row.awarding_agency),
    }


def build_review_rows(
    db: Session,
    limit: int = 50,
    tier: str | None = None,
) -> list[dict]:
    """Query active leads and return review pack rows.

    Read-only — no inserts, updates, deletes, or commits.
    Rows are ordered by tier priority, then score, then award strength.
    """
    min_single = Decimal(os.getenv("MIN_QUALIFYING_SINGLE_AWARD_AMOUNT", "10000"))
    min_90d = Decimal(os.getenv("MIN_QUALIFYING_COMPANY_90D_AWARD_TOTAL", "10000"))

    params: dict = {"limit": limit}
    if tier:
        params["tier"] = tier

    rows = db.execute(text(_leads_query_sql(tier)), params).fetchall()

    result = []
    for rank, row in enumerate(rows, start=1):
        largest = (
            Decimal(str(row.largest_single))
            if row.largest_single is not None
            else Decimal("0")
        )
        recent = (
            Decimal(str(row.recent_total_90d))
            if row.recent_total_90d is not None
            else Decimal("0")
        )
        pos = int(row.positive_count) if row.positive_count else 0

        pass_type = _classify_pass_type(largest, recent, min_single, min_90d)
        why = _build_why(pass_type, largest, recent, pos, row.most_recent_date)
        ev = _fetch_best_evidence(row.company_id, db)

        result.append({
            "rank": rank,
            "company_name": _na(row.canonical_name),
            "tier": _na(row.tier),
            "current_score": _na(row.current_score),
            "review_status": _na(row.sales_status),
            "state": _na(row.state),
            "city": _na(row.city),
            "naics_code": _na(row.naics_code),
            "naics_description": _na(row.naics_description),
            "largest_single_award": _fmt_currency(row.largest_single),
            "award_total_90d": _fmt_currency(row.recent_total_90d),
            "lifetime_award_total": _fmt_currency(row.lifetime_total),
            "positive_award_count": str(pos),
            "most_recent_award_date": _na(row.most_recent_date),
            "gate10_pass_type": pass_type,
            "primary_agency": ev["agency"],
            "best_evidence_claim": ev["claim"],
            "best_evidence_confidence": ev["confidence"],
            "source_url": ev["source_url"],
            "why_this_lead_is_included": why,
            "reviewer_notes": "",
        })

    return result


def count_active_leads(db: Session, tier: str | None = None) -> int:
    """Count active lead_candidates, optionally filtered by tier."""
    tier_clause = "AND lc.tier = :tier" if tier else ""
    sql = dedent(f"""
        SELECT COUNT(lc.id)
        FROM lead_candidates lc
        JOIN companies c ON c.id = lc.company_id
        WHERE lc.status = 'active'
          AND lc.deleted_at IS NULL
          AND c.deleted_at IS NULL
          {tier_clause}
    """).strip()
    if tier:
        result = db.execute(text(sql), {"tier": tier}).scalar()
    else:
        result = db.execute(text(sql)).scalar()
    return int(result) if result else 0


# ── Output writers ────────────────────────────────────────────────────────────

def write_csv(rows: list[dict], output_path: str) -> None:
    """Write review pack rows to a CSV file. Creates parent directories if needed."""
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_markdown(rows: list[dict], meta: dict, output_path: str) -> None:
    """Write review pack to a Markdown file. Creates parent directories if needed."""
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []

    # ── Header ────────────────────────────────────────────────────────────────
    lines.append("# Manual Review Pack — Research-Ready Leads")
    lines.append("")
    lines.append(f"> **CAUTION:** {_CAUTION}")
    lines.append("")

    # ── Summary ───────────────────────────────────────────────────────────────
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- **Generated at:** {meta.get('generated_at', NOT_AVAILABLE)}")
    lines.append(f"- **Database:** {meta.get('database', NOT_AVAILABLE)}")
    lines.append(f"- **Limit:** {meta.get('limit', NOT_AVAILABLE)}")
    lines.append(f"- **Active leads considered:** {meta.get('active_count', NOT_AVAILABLE)}")
    lines.append(f"- **Leads exported:** {meta.get('leads_exported', NOT_AVAILABLE)}")

    tier_dist = meta.get("tier_distribution", {})
    tier_str = ", ".join(
        f"{k}: {v}" for k, v in sorted(tier_dist.items()) if k != NOT_AVAILABLE
    ) or NOT_AVAILABLE
    lines.append(f"- **Tier distribution:** {tier_str}")
    lines.append("")

    # ── Lead cards ────────────────────────────────────────────────────────────
    lines.append("## Lead Cards")
    lines.append("")

    if not rows:
        lines.append("_No active research-ready leads found._")
        lines.append("")
    else:
        for row in rows:
            rank = row.get("rank", NOT_AVAILABLE)
            name = row.get("company_name", NOT_AVAILABLE)
            lines.append(f"### #{rank} — {name}")
            lines.append("")
            tier_val = row.get("tier", NOT_AVAILABLE)
            score_val = row.get("current_score", NOT_AVAILABLE)
            status_val = row.get("review_status", NOT_AVAILABLE)
            lines.append(
                f"**Tier:** {tier_val} | **Score:** {score_val} | **Status:** {status_val}"
            )
            city_val = row.get("city", NOT_AVAILABLE)
            state_val = row.get("state", NOT_AVAILABLE)
            naics_val = row.get("naics_code", NOT_AVAILABLE)
            naics_desc = row.get("naics_description", NOT_AVAILABLE)
            lines.append(
                f"**Location:** {city_val}, {state_val} | "
                f"**NAICS:** {naics_val} — {naics_desc}"
            )
            lines.append("")
            lines.append("**Award Summary:**")
            lines.append(f"- Largest single award: {row.get('largest_single_award', NOT_AVAILABLE)}")
            lines.append(f"- 90-day total: {row.get('award_total_90d', NOT_AVAILABLE)}")
            lines.append(f"- Lifetime total: {row.get('lifetime_award_total', NOT_AVAILABLE)}")
            lines.append(f"- Positive award count: {row.get('positive_award_count', NOT_AVAILABLE)}")
            lines.append(f"- Most recent award: {row.get('most_recent_award_date', NOT_AVAILABLE)}")
            lines.append(f"- Gate 10 pass type: {row.get('gate10_pass_type', NOT_AVAILABLE)}")
            lines.append("")
            lines.append(
                f"**Why included:** {row.get('why_this_lead_is_included', NOT_AVAILABLE)}"
            )
            lines.append("")
            lines.append("**Evidence:**")
            lines.append(f"- Claim: {row.get('best_evidence_claim', NOT_AVAILABLE)}")
            lines.append(f"- Confidence: {row.get('best_evidence_confidence', NOT_AVAILABLE)}")
            lines.append(f"- Agency: {row.get('primary_agency', NOT_AVAILABLE)}")
            lines.append(f"- Source: {row.get('source_url', NOT_AVAILABLE)}")
            lines.append("")
            lines.append("**Reviewer Notes:**")
            lines.append("_(leave blank — for internal use only)_")
            lines.append("")
            lines.append("---")
            lines.append("")

    # ── Limitations ───────────────────────────────────────────────────────────
    lines.append("## Limitations")
    lines.append("")
    for item in _LIMITATIONS:
        lines.append(f"- {item}")
    lines.append("")

    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
        fh.write("\n")
