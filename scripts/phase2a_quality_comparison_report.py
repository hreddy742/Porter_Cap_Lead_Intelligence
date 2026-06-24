"""
Phase 2A Before/After Quality Comparison Report.

Usage:
    python scripts/phase2a_quality_comparison_report.py

Reads the current local database only.
No data is written or modified. No live API calls are made.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.session import SessionLocal
from app.ops.quality_comparison import (
    INTERPRETATION_NOTES,
    build_comparison,
    compute_changes,
)


# ── Formatting helpers ─────────────────────────────────────────────────────────

def _sep(char: str = "-", width: int = 72) -> str:
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


def _fmt_change(n: int) -> str:
    if n > 0:
        return f"+{n}"
    return str(n)


def _table_row(metric: str, before, after, change: str, interp: str) -> None:
    print(f"  {metric:<46} {str(before):>6}  {str(after):>6}  {change:>7}  {interp}")


# ── Report sections ────────────────────────────────────────────────────────────

def _print_comparison_table(report, changes: dict) -> None:
    _section("SECTION 1 — BEFORE vs AFTER: PHASE 2A GATE 10 REPROCESSING")
    print()
    print(
        f"  {'Metric':<46} {'Before':>6}  {'After':>6}  {'Change':>7}  Interpretation"
    )
    print("  " + "-" * 115)

    b = report.before
    a = report.after

    rows = [
        (
            "Active lead candidates",
            b.active_lead_candidates,
            a.active_lead_candidates,
            changes["active_lead_candidates"],
            "Active reviewable leads dropped from 39 to 31.",
        ),
        (
            "Warm leads (active)",
            b.warm,
            a.warm,
            changes["warm"],
            "5 warm leads were below Gate 10 threshold.",
        ),
        (
            "Cold leads (active)",
            b.cold,
            a.cold,
            changes["cold"],
            "3 cold leads were below Gate 10 threshold.",
        ),
        (
            "Gate 10 failing active leads",
            b.gate10_failing,
            a.gate10_failing,
            changes["gate10_failing"],
            "All below-threshold leads removed by reprocessing.",
        ),
        (
            "Pass-only-by-90d active leads",
            b.pass_only_90d,
            a.pass_only_90d,
            changes["pass_only_90d"],
            "Unchanged — 90-day aggregate qualifies.",
        ),
        (
            "Archived by Gate 10 reprocessing",
            b.archived_by_gate10,
            a.archived_by_gate10,
            changes["archived_by_gate10"],
            "Recoverable from review_decisions.",
        ),
        (
            "Leads with largest single award >= $10K",
            b.gate10_passing_single,
            a.gate10_passing_single,
            changes["gate10_passing_single"],
            "Core strong leads preserved.",
        ),
        (
            "Active leads below threshold",
            b.gate10_failing,
            a.gate10_failing,
            changes["gate10_failing"],
            "Phase 2A reduced Gate 10 failing leads to 0.",
        ),
    ]

    for metric, before_val, after_val, chg, interp in rows:
        _table_row(metric, before_val, after_val, _fmt_change(chg), interp)


def _print_archived_leads(report) -> None:
    _section("SECTION 2 — LEADS ARCHIVED BY GATE 10 REPROCESSING")
    count = report.after.archived_by_gate10
    names = report.after.archived_company_names

    print(f"  Archived count (from review_decisions WHERE reviewer_id='system:gate10_reprocess'): {count}")
    print()

    if not names:
        print("  Company names not recoverable — no matching review_decisions rows found.")
        print("  This may mean reprocessing was not applied or was rolled back.")
        return

    print("  Archived company names:")
    for name in names:
        print(f"    - {name}")


def _print_current_snapshot(report) -> None:
    _section("SECTION 3 — CURRENT ACTIVE LEAD QUALITY SNAPSHOT  (live DB)")
    a = report.after
    print(f"  Active lead candidates:              {a.active_lead_candidates}")
    print(f"  Warm:                                {a.warm}")
    print(f"  Cold:                                {a.cold}")
    print(f"  Gate 10 failing (below threshold):   {a.gate10_failing}")
    print(f"  Gate 10 pass-only-by-90d:            {a.pass_only_90d}")
    print(f"  Gate 10 pass via single award:       {a.gate10_passing_single}")


def _print_limitations() -> None:
    _section("SECTION 4 — LIMITATIONS")
    print(
        "  1. 'Before' values are baseline constants from the Phase 2A Gate 10 impact\n"
        "     report captured before reprocessing. They are NOT reconstructed from\n"
        "     the database. The database no longer contains the pre-reprocess active state."
    )
    print(
        "  2. Warm/cold before-split (35 warm / 4 cold) comes from the captured impact\n"
        "     report only and cannot be verified against current DB state."
    )
    print(
        "  3. The 90-day window for pass_only_90d moves with calendar time — the\n"
        "     'after' count may change as award dates age out of the window."
    )
    print(
        "  4. Archived lead counts depend on review_decisions rows written by\n"
        "     system:gate10_reprocess. If that process was not applied or was rolled\n"
        "     back, the archived count will show 0."
    )
    print(
        "  5. No SAM.gov contactability check, scoring, or Salesforce state was\n"
        "     evaluated for this report."
    )


def _print_interpretation() -> None:
    _section("SECTION 5 — HONEST INTERPRETATION")
    for note in INTERPRETATION_NOTES:
        print(f"  - {note}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    _header("PORTER CAPITAL — PHASE 2A QUALITY COMPARISON REPORT")

    print()
    print("  'Before' values: Baseline captured before Gate 10 reprocessing.")
    print("  These are hardcoded constants from the Phase 2A Gate 10 impact report.")
    print("  They are NOT reconstructed from the current database.")
    print()
    print("  'After' values: Computed from the current local database.")

    with SessionLocal() as db:
        report = build_comparison(db)

    changes = compute_changes(report)

    _print_comparison_table(report, changes)
    _print_archived_leads(report)
    _print_current_snapshot(report)
    _print_limitations()
    _print_interpretation()

    print()
    print(_sep("="))
    print("  END OF REPORT  —  No data was written or modified.")
    print(_sep("="))
    print()


if __name__ == "__main__":
    main()
