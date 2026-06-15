"""
Manual Review Pack — Research-Ready Leads.

Usage:
    python scripts/manual_review_pack.py
    python scripts/manual_review_pack.py --limit 25 --tier warm --format csv

Read-only: no inserts, updates, deletes, commits, pipeline runs, or API calls.
"""

import argparse
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.session import SessionLocal
from app.ops.manual_review_pack import (
    NOT_AVAILABLE,
    build_review_rows,
    count_active_leads,
    write_csv,
    write_markdown,
)


def _sep(char: str = "-", width: int = 64) -> str:
    return char * width


def _header(title: str) -> None:
    print()
    print(_sep("="))
    print(f"  {title}")
    print(_sep("="))


def _kv(label: str, value, width: int = 30) -> None:
    print(f"  {label:<{width}} {value}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a manual review pack for active research-ready leads."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum number of leads to export (default: 50).",
    )
    parser.add_argument(
        "--tier",
        choices=["warm", "cold", "all"],
        default="all",
        help="Filter by tier: warm, cold, or all (default: all).",
    )
    parser.add_argument(
        "--output-dir",
        default="outputs/manual_review",
        help="Output directory (default: outputs/manual_review).",
    )
    parser.add_argument(
        "--format",
        choices=["csv", "md", "both"],
        default="both",
        help="Output format: csv, md, or both (default: both).",
    )
    args = parser.parse_args()

    tier_filter = None if args.tier == "all" else args.tier
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    _header("PORTER CAPITAL — MANUAL REVIEW PACK  (Research-Ready Leads)")
    print()
    print("  CAUTION: These are not sales-ready leads.")
    print("  Contacts are not verified. Factoring need is not proven.")
    print("  Human review is required before outreach.")
    print()

    with SessionLocal() as db:
        rows = build_review_rows(db, limit=args.limit, tier=tier_filter)
        active_count = count_active_leads(db, tier=tier_filter)

    tier_distribution = dict(Counter(r["tier"] for r in rows if r["tier"] != NOT_AVAILABLE))
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    database_url = os.getenv("DATABASE_URL", NOT_AVAILABLE)

    meta = {
        "generated_at": generated_at,
        "database": database_url,
        "limit": args.limit,
        "active_count": active_count,
        "leads_exported": len(rows),
        "tier_distribution": tier_distribution,
    }

    csv_path: Path | None = None
    md_path: Path | None = None

    if args.format in ("csv", "both"):
        csv_path = output_dir / f"manual_review_pack_{timestamp}.csv"
        write_csv(rows, str(csv_path))

    if args.format in ("md", "both"):
        md_path = output_dir / f"manual_review_pack_{timestamp}.md"
        write_markdown(rows, meta, str(md_path))

    # ── Print summary ─────────────────────────────────────────────────────────
    print(_sep("-"))
    print("  EXPORT SUMMARY")
    print(_sep("-"))
    _kv("Generated at:", generated_at)
    _kv("Active leads considered:", active_count)
    _kv("Leads exported:", len(rows))
    _kv("Limit:", args.limit)
    _kv("Tier filter:", args.tier)

    if tier_distribution:
        for tier, count in sorted(tier_distribution.items()):
            _kv(f"  tier={tier}:", count)
    else:
        _kv("  (no leads exported)", "")

    print()
    print("  OUTPUT FILES")
    print(_sep("-"))
    if csv_path:
        _kv("CSV:", str(csv_path))
    if md_path:
        _kv("Markdown:", str(md_path))

    if rows:
        print()
        print("  FIRST 5 COMPANIES")
        print(_sep("-"))
        for row in rows[:5]:
            print(
                f"  #{row['rank']:<3}  {row['company_name']:<42}  "
                f"tier={row['tier']:<6}  score={row['current_score']:<6}  "
                f"{row['gate10_pass_type']}"
            )

    print()
    print(_sep("-"))
    print("  NOTE: No database records were modified.")
    print("  NOTE: No live ingestion or external API was run.")
    print(_sep("-"))
    print()


if __name__ == "__main__":
    main()
