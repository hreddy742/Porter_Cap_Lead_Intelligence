"""
Entrypoint for the SAM.gov daily enrichment job.

Usage:
    python scripts/run_sam_enrichment.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.enrichment.sam_gov_daily import run_enrichment

if __name__ == "__main__":
    result = run_enrichment()

    print()
    print("=" * 62)
    print("  SAM.gov enrichment summary")
    print("=" * 62)
    print(f"  Enriched  : {result.get('enriched', 0)}")
    print(f"  Matched   : {result.get('matched', 0)}")
    print(f"  Not found : {result.get('not_found', 0)}")
    print(f"  Rescored  : {result.get('rescored', 0)}")

    samples = result.get("matched_samples", [])
    if samples:
        print()
        print(f"  Sample matches ({len(samples)}):")
        for s in samples:
            print(f"    {s['name']}")
            print(f"      naics={s['naics']}  ceo={s['ceo']}")

    upgrades = result.get("tier_upgrades", [])
    if upgrades:
        print()
        print(f"  Tier upgrades ({len(upgrades)}):")
        for u in upgrades:
            print(f"    {u['name']}  {u['old_tier']} -> {u['new_tier']}  score={u['score']}")
    else:
        print()
        print("  No tier upgrades this run.")

    if result.get("error"):
        print(f"\n  ERROR: {result['error']}")
        sys.exit(1)
