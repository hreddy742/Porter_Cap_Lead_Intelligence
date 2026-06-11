"""
Lead Quality Proof Report — Phase 2A read-only diagnostic.

Usage:
    python scripts/lead_quality_report.py

Reads the local database and prints a formatted quality report to the terminal.
No data is written or modified.
"""

import sys
import os

# Allow running from repo root without installing the package
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.session import SessionLocal
from app.ops.lead_quality import BUCKET_KEYS, build_report


# ─── Formatting helpers ───────────────────────────────────────────────────────

def _sep(char: str = "-", width: int = 60) -> str:
    return char * width


def _header(title: str) -> None:
    print()
    print(_sep("="))
    print(f"  {title}")
    print(_sep("="))


def _section(title: str) -> None:
    print()
    print(title)
    print(_sep("-", len(title)))


def _kv(label: str, value, width: int = 28) -> None:
    print(f"  {label:<{width}} {value}")


# ─── Print sections ───────────────────────────────────────────────────────────

def _print_summary(report) -> None:
    _section("PIPELINE SUMMARY")
    _kv("Raw source records:", report.raw_source_records)
    _kv("Evidence records:", report.evidence_items)
    _kv("Companies:", report.companies)
    _kv("Lead candidates:", report.lead_candidates)


def _print_tier_counts(report) -> None:
    _section("LEAD TIER DISTRIBUTION  (active candidates, lead_candidates.tier)")
    if not report.tier_counts:
        print("  (no active candidates)")
        return
    order = ["hot", "warm", "cold", "archive", "unscored"]
    seen = set()
    for tier in order:
        if tier in report.tier_counts:
            _kv(f"{tier}:", report.tier_counts[tier])
            seen.add(tier)
    for tier, n in report.tier_counts.items():
        if tier not in seen:
            _kv(f"{tier}:", n)


def _print_award_buckets(report) -> None:
    _section("AWARD AMOUNT BUCKETS  (signals WHERE signal_type = 'CONTRACT_AWARD')")
    labels = {
        "negative":    "negative (< $0):",
        "zero":        "zero ($0):",
        "under_50k":   "under $50K  (> $0 and < $50K):",
        "50k_to_250k": "$50K to $250K:",
        "250k_to_1m":  "$250K to $1M:",
        "over_1m":     "over $1M:",
        "null_amount": "null / missing:",
    }
    for key in BUCKET_KEYS:
        _kv(labels[key], report.award_buckets.get(key, 0))


def _print_top_naics(report) -> None:
    _section("TOP NAICS CODES  (by company count)")
    if not report.top_naics:
        print("  (no NAICS data)")
        return
    for row in report.top_naics:
        desc = row["naics_description"] or "(no description)"
        print(f"  {row['naics_code']:<10}  {row['company_count']:>4} companies  {desc}")


def _print_top_agencies(report) -> None:
    _section("TOP AWARDING AGENCIES  (by evidence count)")
    if not report.top_agencies:
        print("  (no agency data)")
        return
    for row in report.top_agencies:
        print(f"  {row['evidence_count']:>4}  {row['agency']}")


def _print_multi_award(report) -> None:
    _section("COMPANIES WITH MULTIPLE RECENT AWARDS  (90-day window, >= 2 awards)")
    if not report.multi_award_companies:
        print("  (none in window)")
        return
    for row in report.multi_award_companies:
        total = f"${row['total_amount']:,.2f}" if row["total_amount"] is not None else "n/a"
        latest = row["latest_award"] or "n/a"
        print(
            f"  {row['award_count']} awards  {total:>14}  latest {latest}  "
            f"{row['canonical_name']}"
        )


def _print_tiny_awards(report) -> None:
    _section("TINY AWARD EXAMPLES  (0 < amount < $50K, sorted ascending)")
    if not report.tiny_award_examples:
        print("  (none found)")
        return
    for row in report.tiny_award_examples:
        amt = f"${row['award_amount']:,.2f}"
        date_str = row["signal_date"] or "n/a"
        agency = row["agency"] or "n/a"
        naics = row["naics_code"] or "n/a"
        print(f"  {amt:>12}  {date_str}  {agency}")
        print(f"               company : {row['canonical_name']}")
        print(f"               NAICS   : {naics}")
        print(f"               evidence: {row['evidence_id']}")
        print(f"               url     : {row['source_url']}")
        print()


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    _header("PORTER CAPITAL - LEAD QUALITY PROOF REPORT  (Phase 2A)")

    with SessionLocal() as db:
        report = build_report(db)

    _print_summary(report)
    _print_tier_counts(report)
    _print_award_buckets(report)
    _print_top_naics(report)
    _print_top_agencies(report)
    _print_multi_award(report)
    _print_tiny_awards(report)

    print()
    print(_sep("="))
    print("  END OF REPORT")
    print(_sep("="))
    print()


if __name__ == "__main__":
    main()
