"""
Contactability-Lite enrichment runner.

Usage:
    python scripts/contactability_lite.py --limit 25 --dry-run
    python scripts/contactability_lite.py --limit 50 --source sam --apply
    python scripts/contactability_lite.py --company-id <uuid> --apply
    python scripts/contactability_lite.py --tier warm --limit 200 --source all --apply

Safety gates:
  * --apply must be explicitly passed to write results (otherwise dry-run is forced).
  * --limit is required for batch runs (omit only when using --company-id).

Read-only / dry-run: no DB writes, no HTTP calls, no output files generated.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.session import SessionLocal
from app.enrichment.orchestrator import run_contactability_enrichment


def _sep(char: str = "-", width: int = 64) -> str:
    return char * width


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Enrich active lead candidates with contactability data.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max companies to enrich. Required for batch runs.",
    )
    parser.add_argument(
        "--company-id",
        default=None,
        help="Enrich exactly one company by UUID (overrides --limit and --tier).",
    )
    parser.add_argument(
        "--tier",
        choices=["warm", "cold", "all"],
        default="warm",
        help="Filter by tier: warm, cold, or all (default: warm).",
    )
    parser.add_argument(
        "--source",
        choices=["search", "sam", "all"],
        default="all",
        help="Which sources to run: search, sam, or all (default: all).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen; no writes, no HTTP calls.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write results to DB. Required to leave dry-run mode.",
    )
    parser.add_argument(
        "--output-dir",
        default="outputs/contactability",
        help="Write enrichment summary here (default: outputs/contactability).",
    )
    parser.add_argument(
        "--format",
        choices=["json", "csv", "both"],
        default="both",
        dest="output_format",
        help="Output format: json, csv, or both (default: both).",
    )
    args = parser.parse_args()

    # Batch run requires --limit unless --company-id given
    if args.company_id is None and args.limit is None:
        print(
            "ERROR: --limit is required for batch runs. "
            "Use --company-id to enrich a single company."
        )
        sys.exit(1)

    # Without --apply, force dry-run regardless of --dry-run flag
    if not args.apply:
        args.dry_run = True

    tier_filter = None if args.tier == "all" else args.tier
    effective_limit = args.limit if args.limit is not None else 1

    print()
    print(_sep("="))
    print("  PORTER CAPITAL — Contactability-Lite Enrichment")
    print(_sep("="))
    print()

    if args.dry_run:
        print("  MODE: DRY RUN (no writes, no HTTP calls)")
        print("  Pass --apply to write results.")
    else:
        print("  MODE: APPLY (writing results to DB)")
    print()

    with SessionLocal() as db:
        result = run_contactability_enrichment(
            db=db,
            limit=effective_limit,
            company_id=args.company_id,
            tier=tier_filter,
            source=args.source,
            dry_run=args.dry_run,
            apply=args.apply,
        )

    print(_sep())
    if result["dry_run"]:
        print(
            f"  Would enrich {result['companies_would_enrich']} companies "
            f"(dry run — no writes, no HTTP calls)"
        )
        print(f"  Tier filter:  {args.tier}")
        print(f"  Source:       {args.source}")
        print(f"  Limit:        {args.limit}")
    else:
        print(f"  Enrichment complete.")
        print(f"  Companies attempted: {result['companies_attempted']}")
        print(f"  Companies enriched:  {result['companies_enriched']}")
        print(f"  Companies failed:    {result['companies_failed']}")
        print(f"  Run ID:              {result.get('run_id', 'N/A')}")

        if args.output_format in ("json", "both"):
            output_dir = Path(args.output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            out_path = output_dir / f"contactability_{timestamp}.json"
            serializable = {k: str(v) if not isinstance(v, (str, int, bool, type(None))) else v
                            for k, v in result.items()}
            with open(out_path, "w", encoding="utf-8") as fh:
                json.dump(serializable, fh, indent=2)
            print(f"  Summary written: {out_path}")

    print(_sep())
    print()


if __name__ == "__main__":
    main()
