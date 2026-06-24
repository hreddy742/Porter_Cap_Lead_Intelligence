"""
Gate 10 Reprocessing Script — Phase 2A controlled reprocessing.

Evaluates existing active lead_candidates against Gate 10 (award amount quality
gate).  Default mode is dry-run: no data is written.

Usage:

  Dry-run (default — zero writes):
      python scripts/reprocess_gate10_existing_leads.py

  Apply mode (writes to database — review dry-run output first):
      python scripts/reprocess_gate10_existing_leads.py --apply

Apply mode updates the following fields on each lead that fails Gate 10:
    status        = 'archived'
    gate_result   = 'archived'
    gate_reason   = 'award_amount_too_small'
    tier          = 'archive'
    current_score = NULL

It also inserts one ReviewDecision audit row per archived lead.
A database backup is recommended before running --apply.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.session import SessionLocal
from app.ops.gate10_reprocess import Gate10ReprocessResult, run_gate10_reprocess


# ─── Output helpers ──────────────────────────────────────────────────────────


def _sep(char: str = "-", width: int = 62) -> str:
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


def _kv(label: str, value, width: int = 44) -> None:
    print(f"  {label:<{width}} {value}")


# ─── Report printer ───────────────────────────────────────────────────────────


def _print_results(result: Gate10ReprocessResult) -> None:
    mode = "DRY-RUN  (no writes)" if result.dry_run else "APPLY MODE"
    _header(f"PORTER CAPITAL — GATE 10 REPROCESSING  [{mode}]")

    _section("OVERVIEW")
    _kv("Active lead candidates scanned:", result.total_active)
    _kv("Would pass Gate 10:", len(result.passing))
    _kv("Would fail Gate 10 (award_amount_too_small):", len(result.failing))
    _kv("Pass only by 90-day aggregation:", len(result.pass_only_90d))

    if result.failing:
        label = (
            "LEADS THAT FAIL GATE 10  (no writes in dry-run)"
            if result.dry_run
            else "LEADS ARCHIVED BY APPLY MODE"
        )
        _section(label)
        for entry in result.failing:
            print(f"  {entry['canonical_name']}")
            print(f"    tier before        : {entry['tier_before']}")
            print(f"    largest single     : ${entry['largest_single']:,.2f}")
            print(f"    90-day total       : ${entry['recent_total_90d']:,.2f}")
            print(f"    most recent award  : {entry['most_recent_award_date'] or 'n/a'}")
            if result.dry_run:
                print("    would set          : status='archived'  gate_result='archived'")
                print("                         gate_reason='award_amount_too_small'")
                print("                         tier='archive'  current_score=NULL")
            print()
    else:
        _section("LEADS THAT FAIL GATE 10")
        print("  (none — all active leads pass Gate 10)")

    if result.pass_only_90d:
        _section("LEADS PASSING ONLY BY 90-DAY AGGREGATION  (not archived)")
        for entry in result.pass_only_90d:
            print(f"  {entry['canonical_name']}")
            print(f"    tier               : {entry['tier_before']}")
            print(f"    largest single     : ${entry['largest_single']:,.2f}")
            print(f"    90-day total       : ${entry['recent_total_90d']:,.2f}")
            print()

    if not result.dry_run:
        _section("APPLY RESULT")
        _kv("Lead candidates archived:", result.applied)
        _kv("ReviewDecision rows inserted:", result.applied)
        print()
        print("  Fields set on each archived lead:")
        print("    status        = 'archived'")
        print("    gate_result   = 'archived'")
        print("    gate_reason   = 'award_amount_too_small'")
        print("    tier          = 'archive'")
        print("    current_score = NULL")
    else:
        print()
        print(_sep("-"))
        print("  DRY-RUN: zero rows were written, updated, or deleted.")
        print("  Run with --apply to write these changes to the database.")
        print(_sep("-"))

    print()
    print(_sep("="))
    print("  END OF REPORT")
    print(_sep("="))
    print()


# ─── Entry point ─────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Gate 10 reprocessing for existing active lead candidates."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help=(
            "Write changes to the database. "
            "Default: dry-run only (zero writes)."
        ),
    )
    args = parser.parse_args()
    dry_run = not args.apply

    db = SessionLocal()
    try:
        if dry_run:
            result = run_gate10_reprocess(db, dry_run=True)
        else:
            result = run_gate10_reprocess(db, dry_run=False)
            db.commit()
    except Exception as exc:
        db.rollback()
        print()
        print("ERROR — reprocessing failed. All changes have been rolled back.")
        print(f"  {exc}")
        print()
        sys.exit(1)
    finally:
        db.close()

    _print_results(result)


if __name__ == "__main__":
    main()
