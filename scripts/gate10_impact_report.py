"""
Gate 10 Before/After Impact Report — Phase 2A read-only diagnostic.

Usage:
    python scripts/gate10_impact_report.py

Reads the local database and prints a formatted impact report to the terminal.
No data is written or modified.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.session import SessionLocal
from app.ops.gate_impact import build_gate10_impact_report


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


def _kv(label: str, value, width: int = 36) -> None:
    print(f"  {label:<{width}} {value}")


def _print_thresholds(report) -> None:
    _section("GATE 10 THRESHOLDS")
    _kv("Min single award:", f"${report.min_single_threshold:,.2f}")
    _kv("Min 90-day total:", f"${report.min_90d_threshold:,.2f}")


def _print_overview(report) -> None:
    _section("OVERVIEW  (active lead_candidates, status='active', deleted_at IS NULL)")
    _kv("Total active lead candidates:", report.total_lead_candidates)
    _kv("Would PASS Gate 10:", report.total_pass)
    _kv("Would FAIL Gate 10 (award_amount_too_small):", report.total_fail)
    _kv("Pass only by 90-day aggregation:", report.total_pass_only_90d)


def _print_tier_counts(report) -> None:
    _section("CURRENT TIER COUNTS  (before Gate 10 impact)")
    if not report.tier_counts_before:
        print("  (no active candidates)")
        return
    order = ["hot", "warm", "cold", "archive", "unscored"]
    seen: set[str] = set()
    for tier in order:
        if tier in report.tier_counts_before:
            _kv(f"{tier}:", report.tier_counts_before[tier])
            seen.add(tier)
    for tier, n in report.tier_counts_before.items():
        if tier not in seen:
            _kv(f"{tier}:", n)


def _print_tier_breakdown(report) -> None:
    _section("GATE 10 IMPACT BY TIER")
    if not report.tier_breakdown:
        print("  (no data)")
        return
    order = ["hot", "warm", "cold", "archive", "unscored"]
    seen: set[str] = set()
    for tier in order:
        if tier in report.tier_breakdown:
            bd = report.tier_breakdown[tier]
            _kv(f"{tier} — would pass:", bd.get("pass", 0))
            _kv(f"{tier} — would fail:", bd.get("fail", 0))
            seen.add(tier)
    for tier, bd in report.tier_breakdown.items():
        if tier not in seen:
            _kv(f"{tier} — would pass:", bd.get("pass", 0))
            _kv(f"{tier} — would fail:", bd.get("fail", 0))


def _print_top_failing(report) -> None:
    _section(
        "TOP COMPANIES THAT WOULD FAIL GATE 10  "
        "(largest single award ascending, max 20)"
    )
    if not report.top_failing:
        print("  (none)")
        return
    for row in report.top_failing:
        print(f"  {row['canonical_name']}")
        print(f"    tier                : {row['tier']}")
        print(f"    largest single award: ${row['largest_single']:,.2f}")
        print(f"    90-day total        : ${row['recent_total_90d']:,.2f}")
        print(f"    positive award count: {row['positive_award_count']}")
        print(f"    most recent award   : {row['most_recent_award_date'] or 'n/a'}")
        print()


def _print_pass_only_90d(report) -> None:
    _section(
        "COMPANIES PASSING ONLY BY 90-DAY AGGREGATION  "
        "(90-day total descending, max 20)"
    )
    if not report.top_pass_only_90d:
        print("  (none)")
        return
    for row in report.top_pass_only_90d:
        print(f"  {row['canonical_name']}")
        print(f"    tier                : {row['tier']}")
        print(f"    largest single award: ${row['largest_single']:,.2f}")
        print(f"    90-day total        : ${row['recent_total_90d']:,.2f}")
        print(f"    positive award count: {row['positive_award_count']}")
        print(f"    most recent award   : {row['most_recent_award_date'] or 'n/a'}")
        print()


def main() -> None:
    _header("PORTER CAPITAL — GATE 10 BEFORE/AFTER IMPACT REPORT  (Phase 2A)")

    with SessionLocal() as db:
        report = build_gate10_impact_report(db)

    _print_thresholds(report)
    _print_overview(report)
    _print_tier_counts(report)
    _print_tier_breakdown(report)
    _print_top_failing(report)
    _print_pass_only_90d(report)

    print()
    print(_sep("-"))
    print("  NOTE: This report does not modify existing leads.")
    print("  No lead_candidates rows were updated, inserted, or deleted.")
    print(_sep("-"))
    print()
    print(_sep("="))
    print("  END OF REPORT")
    print(_sep("="))
    print()


if __name__ == "__main__":
    main()
