"""Seed sba_loans source in source_registry.

Revision ID: 006_add_sba_loans_source
Revises: 005_add_sector_excluded_flag

Adds the SBA 7(a) FOIA loan connector as Porter's third lead source.
enabled=False by default — enable only after local UAT.

Source notes:
  - Bulk CSV download from data.sba.gov (FOIA data, updated quarterly)
  - ~150 MB file, cached locally for 30 days
  - Covers ALL B2B industries per John Cox Miller confirmation June 25 2026
  - Excludes pure B2C: retail (NAICS 44/45), restaurants/hotels (NAICS 72)
  - Signal types: SBA_LOAN_PIF (paid-in-full) and SBA_LOAN_ACTIVE (active lien)
  - State filter: AL, GA, TN, FL, MS, TX, VA
  - Amount filter: $50K – $5M
  - Date filter: 2022-01-01 or later
"""

import uuid as uuid_module

from alembic import op
import sqlalchemy as sa

revision = "006_add_sba_loans_source"
down_revision = "005_add_sector_excluded_flag"
branch_labels = None
depends_on = None

_SOURCE_ID = uuid_module.UUID("00000000-0000-0000-0000-000000000003")


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text("""
            INSERT INTO source_registry
                (id, name, category, access_method, status, enabled, cost_type,
                 legal_notes, signal_types, base_url, created_at, updated_at)
            SELECT
                CAST(:id AS uuid),
                :name,
                :category,
                :access_method,
                :status,
                :enabled,
                :cost_type,
                :legal_notes,
                ARRAY['SBA_LOAN_PIF', 'SBA_LOAN_ACTIVE']::varchar[],
                :base_url,
                NOW(),
                NOW()
            WHERE NOT EXISTS (
                SELECT 1 FROM source_registry WHERE name = :name
            )
        """),
        {
            "id": str(_SOURCE_ID),
            "name": "sba_loans",
            "category": "government_loan",
            "access_method": "bulk_csv",
            "status": "planned",
            "enabled": False,
            "cost_type": "free",
            "legal_notes": (
                "Public domain US government data (SBA 7(a) FOIA disclosures). "
                "No ToS restrictions on use. Bulk CSV updated quarterly by SBA. "
                "Enable SBA_LOANS_ENABLED=true in env after local UAT. "
                "Confirmed by John Cox Miller, Porter Capital, June 25 2026: "
                "include ALL B2B industries, exclude only pure B2C."
            ),
            "base_url": "https://data.sba.gov/dataset/7-a-504-foia",
        },
    )


def downgrade() -> None:
    conn = op.get_bind()
    # Must delete child rows before parent rows.  Dependency order:
    #   signals (→ evidence_items AND → source_registry)
    #   evidence_items (→ raw_source_events AND → source_registry)
    #   raw_source_events (→ source_registry)
    #   source_runs (→ source_registry)
    #   company_identifiers.source_id (nullable, null it out)
    #   source_registry
    conn.execute(
        sa.text("DELETE FROM signals WHERE source_id = CAST(:id AS uuid)"),
        {"id": str(_SOURCE_ID)},
    )
    conn.execute(
        sa.text("DELETE FROM evidence_items WHERE source_id = CAST(:id AS uuid)"),
        {"id": str(_SOURCE_ID)},
    )
    conn.execute(
        sa.text("DELETE FROM raw_source_events WHERE source_id = CAST(:id AS uuid)"),
        {"id": str(_SOURCE_ID)},
    )
    conn.execute(
        sa.text("DELETE FROM source_runs WHERE source_id = CAST(:id AS uuid)"),
        {"id": str(_SOURCE_ID)},
    )
    conn.execute(
        sa.text(
            "UPDATE company_identifiers SET source_id = NULL "
            "WHERE source_id = CAST(:id AS uuid)"
        ),
        {"id": str(_SOURCE_ID)},
    )
    conn.execute(
        sa.text("DELETE FROM source_registry WHERE id = CAST(:id AS uuid)"),
        {"id": str(_SOURCE_ID)},
    )
