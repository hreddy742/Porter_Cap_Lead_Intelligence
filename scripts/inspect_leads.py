"""DB inspection: lead pipeline state after run."""
from app.db.session import SessionLocal
from sqlalchemy import text

with SessionLocal() as db:
    print("=== lead_candidates count ===")
    r = db.execute(text("SELECT COUNT(*) FROM lead_candidates")).scalar()
    print(r)

    print("\n=== lead_scores count ===")
    r = db.execute(text("SELECT COUNT(*) FROM lead_scores")).scalar()
    print(r)

    print("\n=== evidence_items freshness (all rows) ===")
    rows = db.execute(text(
        "SELECT id, claim_supported, freshness_score, "
        "extracted_fields->>'action_date' AS action_date "
        "FROM evidence_items ORDER BY freshness_score DESC LIMIT 20"
    )).fetchall()
    for r in rows:
        print(r)

    print("\n=== signals freshness (all rows) ===")
    rows = db.execute(text(
        "SELECT id, signal_type, signal_date, freshness_score, award_amount "
        "FROM signals ORDER BY freshness_score DESC LIMIT 20"
    )).fetchall()
    for r in rows:
        print(r)

    print("\n=== companies (sample) ===")
    rows = db.execute(text(
        "SELECT id, canonical_name, naics_code, country, business_type "
        "FROM companies LIMIT 10"
    )).fetchall()
    for r in rows:
        print(r)
