"""
Read-only lead quality report service.

Queries the database and returns structured metrics — never writes, never
modifies pipeline state, never touches scoring or gate logic.

Note on lead_candidates.tier: the column is named `tier` in the schema
(not `current_tier`). It holds the most-recent scored tier for the candidate.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

# Ordered bucket keys used throughout (null last so positive buckets read top→down)
BUCKET_KEYS = (
    "negative",
    "zero",
    "under_50k",
    "50k_to_250k",
    "250k_to_1m",
    "over_1m",
    "null_amount",
)


# ─── Pure helper (no DB) ──────────────────────────────────────────────────────

def classify_award_bucket(amount: Decimal | None) -> str:
    """Return the bucket name for a single award amount.

    Boundaries:
      negative    : amount < 0
      zero        : amount == 0
      under_50k   : 0 < amount < 50 000
      50k_to_250k : 50 000 <= amount < 250 000
      250k_to_1m  : 250 000 <= amount < 1 000 000
      over_1m     : amount >= 1 000 000
      null_amount : amount is None
    """
    if amount is None:
        return "null_amount"
    amount = Decimal(str(amount))
    if amount < 0:
        return "negative"
    if amount == 0:
        return "zero"
    if amount < Decimal("50000"):
        return "under_50k"
    if amount < Decimal("250000"):
        return "50k_to_250k"
    if amount < Decimal("1000000"):
        return "250k_to_1m"
    return "over_1m"


# ─── Report dataclass ─────────────────────────────────────────────────────────

@dataclass
class LeadQualityReport:
    raw_source_records: int
    evidence_items: int
    companies: int
    lead_candidates: int
    tier_counts: dict[str, int] = field(default_factory=dict)
    award_buckets: dict[str, int] = field(default_factory=dict)
    top_naics: list[dict] = field(default_factory=list)
    top_agencies: list[dict] = field(default_factory=list)
    multi_award_companies: list[dict] = field(default_factory=list)
    tiny_award_examples: list[dict] = field(default_factory=list)
    action_type_distribution: list[dict] = field(default_factory=list)
    company_award_aggregation: list[dict] = field(default_factory=list)


# ─── Individual query functions ───────────────────────────────────────────────

def count_raw_source_records(db: Session) -> int:
    return db.execute(text("SELECT COUNT(*) FROM raw_source_events")).scalar() or 0


def count_evidence_items(db: Session) -> int:
    return db.execute(text("SELECT COUNT(*) FROM evidence_items")).scalar() or 0


def count_companies(db: Session) -> int:
    return (
        db.execute(text("SELECT COUNT(*) FROM companies WHERE deleted_at IS NULL")).scalar()
        or 0
    )


def count_lead_candidates(db: Session) -> int:
    return (
        db.execute(text("SELECT COUNT(*) FROM lead_candidates WHERE deleted_at IS NULL")).scalar()
        or 0
    )


def get_tier_counts(db: Session) -> dict[str, int]:
    """Count active lead_candidates by their current tier (lead_candidates.tier).

    Candidates that have not yet been scored appear under the key 'unscored'.
    Only rows with status='active' and no soft-delete are counted.
    """
    rows = db.execute(
        text("""
            SELECT COALESCE(tier, 'unscored') AS tier, COUNT(*) AS n
            FROM lead_candidates
            WHERE status = 'active' AND deleted_at IS NULL
            GROUP BY tier
        """)
    ).fetchall()
    return {row.tier: int(row.n) for row in rows}


def get_award_buckets(db: Session) -> dict[str, int]:
    """Count CONTRACT_AWARD signals by award_amount bucket.

    NULL award_amounts are counted separately in the 'null_amount' key.
    Filters to signal_type = 'CONTRACT_AWARD' only.
    """
    row = db.execute(
        text("""
            SELECT
              COUNT(*) FILTER (WHERE award_amount IS NULL)                              AS cnt_null,
              COUNT(*) FILTER (WHERE award_amount < 0)                                  AS cnt_negative,
              COUNT(*) FILTER (WHERE award_amount = 0)                                  AS cnt_zero,
              COUNT(*) FILTER (WHERE award_amount > 0 AND award_amount < 50000)         AS cnt_under_50k,
              COUNT(*) FILTER (WHERE award_amount >= 50000  AND award_amount < 250000)  AS cnt_50k,
              COUNT(*) FILTER (WHERE award_amount >= 250000 AND award_amount < 1000000) AS cnt_250k,
              COUNT(*) FILTER (WHERE award_amount >= 1000000)                           AS cnt_over_1m
            FROM signals
            WHERE signal_type = 'CONTRACT_AWARD'
        """)
    ).fetchone()

    if row is None:
        return {k: 0 for k in BUCKET_KEYS}

    return {
        "negative":    int(row.cnt_negative),
        "zero":        int(row.cnt_zero),
        "under_50k":   int(row.cnt_under_50k),
        "50k_to_250k": int(row.cnt_50k),
        "250k_to_1m":  int(row.cnt_250k),
        "over_1m":     int(row.cnt_over_1m),
        "null_amount": int(row.cnt_null),
    }


def get_top_naics(db: Session, limit: int = 10) -> list[dict]:
    """Return top NAICS codes by company count.

    Primary source: companies.naics_code + companies.naics_description.
    Fallback: if naics_description is NULL on the company row, queries
    evidence_items.extracted_fields->>'naics_description' for that code.
    """
    rows = db.execute(
        text("""
            SELECT
              naics_code,
              MAX(naics_description) AS naics_description,
              COUNT(*)               AS company_count
            FROM companies
            WHERE naics_code IS NOT NULL AND deleted_at IS NULL
            GROUP BY naics_code
            ORDER BY company_count DESC
            LIMIT :lim
        """),
        {"lim": limit},
    ).fetchall()

    results = []
    for row in rows:
        desc = row.naics_description
        if desc is None:
            # Fallback: look for description in evidence_items extracted_fields
            fb = db.execute(
                text("""
                    SELECT extracted_fields->>'naics_description' AS desc
                    FROM evidence_items
                    WHERE extracted_fields->>'naics_code' = :code
                      AND extracted_fields->>'naics_description' IS NOT NULL
                    LIMIT 1
                """),
                {"code": row.naics_code},
            ).scalar()
            desc = fb  # may still be None if truly unknown
        results.append(
            {
                "naics_code": row.naics_code,
                "naics_description": desc,
                "company_count": int(row.company_count),
            }
        )
    return results


def get_top_agencies(db: Session, limit: int = 10) -> list[dict]:
    """Return top awarding agencies by evidence count.

    Source: evidence_items.extracted_fields->>'awarding_agency'.
    Null/blank agency values are excluded.
    """
    rows = db.execute(
        text("""
            SELECT
              extracted_fields->>'awarding_agency' AS agency,
              COUNT(*)                              AS evidence_count
            FROM evidence_items
            WHERE extracted_fields->>'awarding_agency' IS NOT NULL
              AND extracted_fields->>'awarding_agency' <> ''
            GROUP BY agency
            ORDER BY evidence_count DESC
            LIMIT :lim
        """),
        {"lim": limit},
    ).fetchall()
    return [{"agency": row.agency, "evidence_count": int(row.evidence_count)} for row in rows]


def get_multi_award_companies(
    db: Session, window_days: int = 90, min_awards: int = 2
) -> list[dict]:
    """Return companies with >= min_awards CONTRACT_AWARD signals in the last window_days.

    Uses signals.signal_date (derived from extracted_fields.action_date).
    Report-only — does not create tables or modify any data.
    """
    rows = db.execute(
        text("""
            SELECT
              c.canonical_name,
              c.id            AS company_id,
              COUNT(s.id)     AS award_count,
              SUM(s.award_amount) AS total_amount,
              MAX(s.signal_date)  AS latest_award
            FROM signals s
            JOIN companies c ON c.id = s.company_id
            WHERE s.signal_type = 'CONTRACT_AWARD'
              AND s.signal_date >= CURRENT_DATE - (:days * INTERVAL '1 day')
            GROUP BY c.id, c.canonical_name
            HAVING COUNT(s.id) >= :min_awards
            ORDER BY award_count DESC, total_amount DESC NULLS LAST
            LIMIT 20
        """),
        {"days": window_days, "min_awards": min_awards},
    ).fetchall()

    return [
        {
            "canonical_name": row.canonical_name,
            "company_id":     str(row.company_id),
            "award_count":    int(row.award_count),
            "total_amount":   float(row.total_amount) if row.total_amount is not None else None,
            "latest_award":   str(row.latest_award) if row.latest_award is not None else None,
        }
        for row in rows
    ]


def get_tiny_award_examples(
    db: Session, threshold: int = 50_000, limit: int = 10
) -> list[dict]:
    """Return positive tiny CONTRACT_AWARD examples (0 < amount < threshold).

    Joins signals → companies → evidence_items to pull agency, NAICS, and source URL.
    The $50.50 Baltimore Auto Supply example will appear here if it is in the local DB.
    """
    rows = db.execute(
        text("""
            SELECT
              c.canonical_name,
              s.award_amount,
              s.signal_date,
              ei.extracted_fields->>'awarding_agency' AS agency,
              c.naics_code,
              c.naics_description,
              s.evidence_id,
              ei.source_url
            FROM signals s
            JOIN companies c  ON c.id  = s.company_id
            JOIN evidence_items ei ON ei.id = s.evidence_id
            WHERE s.signal_type = 'CONTRACT_AWARD'
              AND s.award_amount > 0
              AND s.award_amount < :threshold
            ORDER BY s.award_amount ASC
            LIMIT :lim
        """),
        {"threshold": threshold, "lim": limit},
    ).fetchall()

    return [
        {
            "canonical_name": row.canonical_name,
            "award_amount":   float(row.award_amount),
            "signal_date":    str(row.signal_date) if row.signal_date is not None else None,
            "agency":         row.agency,
            "naics_code":     row.naics_code,
            "naics_description": row.naics_description,
            "evidence_id":    str(row.evidence_id),
            "source_url":     row.source_url,
        }
        for row in rows
    ]


def get_action_type_distribution(db: Session) -> list[dict]:
    """Return USASpending action type distribution from evidence_items.

    Reads extracted_fields->>'action_type' and extracted_fields->>'action_type_description'.
    NULL action_type values (from rows fetched before this field was requested) appear
    as 'unknown'. Award amount stats come from the signals table (typed Numeric column)
    via LEFT JOIN so evidence items without a resolved signal are still counted.
    Display-only — never used for scoring, gating, or filtering.
    """
    rows = db.execute(
        text("""
            SELECT
              COALESCE(ei.extracted_fields->>'action_type', 'unknown')        AS action_type,
              MAX(ei.extracted_fields->>'action_type_description')            AS action_type_description,
              COUNT(DISTINCT ei.id)                                            AS count,
              SUM(s.award_amount)                                              AS total_award_amount,
              AVG(s.award_amount)                                              AS avg_award_amount,
              COUNT(s.id) FILTER (
                WHERE s.award_amount > 0 AND s.award_amount < 50000
              )                                                                AS tiny_award_count
            FROM evidence_items ei
            LEFT JOIN signals s ON s.evidence_id = ei.id
              AND s.signal_type = 'CONTRACT_AWARD'
            WHERE ei.claim_supported = 'CONTRACT_AWARD'
            GROUP BY ei.extracted_fields->>'action_type'
            ORDER BY count DESC
        """)
    ).fetchall()

    return [
        {
            "action_type": row.action_type,
            "action_type_description": row.action_type_description,
            "count": int(row.count),
            "total_award_amount": float(row.total_award_amount) if row.total_award_amount is not None else None,
            "avg_award_amount": float(row.avg_award_amount) if row.avg_award_amount is not None else None,
            "tiny_award_count": int(row.tiny_award_count) if row.tiny_award_count is not None else 0,
        }
        for row in rows
    ]


# ─── Company award aggregation ────────────────────────────────────────────────

def get_company_award_aggregation(db: Session, limit: int = 20) -> list[dict]:
    """Per-company award aggregation for active lead candidates only.

    Scope: lead_candidates WHERE status='active' AND deleted_at IS NULL,
           joined with companies WHERE deleted_at IS NULL.
    Only positive award amounts count (zero/negative/null excluded).
    90-day total: signal_date >= CURRENT_DATE - 90 days.
    pass_type mirrors Gate 10 logic — display-only, never used for scoring or gating.

    Returns up to `limit` rows ordered by lifetime_total DESC NULLS LAST.
    """
    _min_single = Decimal(os.getenv("MIN_QUALIFYING_SINGLE_AWARD_AMOUNT", "10000"))
    _min_90d = Decimal(os.getenv("MIN_QUALIFYING_COMPANY_90D_AWARD_TOTAL", "10000"))

    rows = db.execute(
        text("""
            SELECT
              c.canonical_name,
              lc.tier,
              COUNT(s.id)         FILTER (WHERE s.award_amount > 0)                AS positive_count,
              MAX(s.award_amount)  FILTER (WHERE s.award_amount > 0)               AS largest_single,
              SUM(s.award_amount)  FILTER (
                WHERE s.award_amount > 0
                  AND s.signal_date >= CURRENT_DATE - (90 * INTERVAL '1 day')
              )                                                                     AS recent_total_90d,
              SUM(s.award_amount)  FILTER (WHERE s.award_amount > 0)               AS lifetime_total,
              MAX(s.signal_date)                                                    AS most_recent_date
            FROM lead_candidates lc
            JOIN companies c ON c.id = lc.company_id
            LEFT JOIN signals s
              ON s.company_id = lc.company_id
              AND s.signal_type = 'CONTRACT_AWARD'
            WHERE lc.status = 'active'
              AND lc.deleted_at IS NULL
              AND c.deleted_at IS NULL
            GROUP BY c.canonical_name, lc.tier, lc.id
            ORDER BY lifetime_total DESC NULLS LAST
            LIMIT :lim
        """),
        {"lim": limit},
    ).fetchall()

    result = []
    for row in rows:
        positive_count = int(row.positive_count) if row.positive_count else 0
        largest = Decimal(str(row.largest_single)) if row.largest_single is not None else Decimal("0")
        recent = Decimal(str(row.recent_total_90d)) if row.recent_total_90d is not None else Decimal("0")

        if positive_count == 0:
            pass_type = "unknown"
        elif largest >= _min_single:
            pass_type = "single_award_pass"
        elif recent >= _min_90d:
            pass_type = "aggregate_90d_pass"
        else:
            pass_type = "below_threshold"

        result.append({
            "canonical_name": row.canonical_name,
            "tier": row.tier,
            "positive_count": positive_count,
            "largest_single": float(largest),
            "recent_total_90d": float(recent),
            "lifetime_total": float(row.lifetime_total) if row.lifetime_total is not None else None,
            "most_recent_date": str(row.most_recent_date) if row.most_recent_date is not None else None,
            "pass_type": pass_type,
        })

    return result


# ─── Report builder ───────────────────────────────────────────────────────────

def build_report(db: Session) -> LeadQualityReport:
    """Assemble all metrics into a single LeadQualityReport."""
    return LeadQualityReport(
        raw_source_records=count_raw_source_records(db),
        evidence_items=count_evidence_items(db),
        companies=count_companies(db),
        lead_candidates=count_lead_candidates(db),
        tier_counts=get_tier_counts(db),
        award_buckets=get_award_buckets(db),
        top_naics=get_top_naics(db),
        top_agencies=get_top_agencies(db),
        multi_award_companies=get_multi_award_companies(db),
        tiny_award_examples=get_tiny_award_examples(db),
        action_type_distribution=get_action_type_distribution(db),
        company_award_aggregation=get_company_award_aggregation(db),
    )
