"""Seed sbir_grants source in source_registry.

Revision ID: 007_add_sbir_grants_source
Revises: 006_add_sba_loans_source

Adds the SBIR/STTR grants connector as Porter's fourth lead source.
enabled=False by default — enable only after local UAT.

Source notes:
  - Public API: https://api.www.sbir.gov/public/api/awards (no API key required)
  - Covers SBIR (Small Business Innovation Research) and STTR awards
  - State filter: AL, GA, TN, FL, MS, TX, VA
  - Amount filter: >= $50K
  - Year filter: 2022+
  - University/academic institution filter applied in connector
  - Signal type: SBIR_GRANT (Phase II/III → strong, Phase I → medium)
  - Confirmed by John Cox Miller, Porter Capital, July 2026
"""

import uuid as uuid_module

from alembic import op
import sqlalchemy as sa

revision = "007_add_sbir_grants_source"
down_revision = "006_add_sba_loans_source"
branch_labels = None
depends_on = None

_SOURCE_ID = uuid_module.UUID("00000000-0000-0000-0000-000000000004")


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
                ARRAY['SBIR_GRANT']::varchar[],
                :base_url,
                NOW(),
                NOW()
            WHERE NOT EXISTS (
                SELECT 1 FROM source_registry WHERE name = :name
            )
        """),
        {
            "id": str(_SOURCE_ID),
            "name": "sbir_grants",
            "category": "government_grant",
            "access_method": "api",
            "status": "planned",
            "enabled": False,
            "cost_type": "free",
            "legal_notes": (
                "Public domain US government data (SBIR/STTR federal grant awards). "
                "No ToS restrictions on use. Free public API, no API key required. "
                "Enable SBIR_GRANTS_ENABLED=true in env after local UAT. "
                "Confirmed by John Cox Miller, Porter Capital, July 2026: "
                "SBIR companies invoice the government — direct A/R financing fit."
            ),
            "base_url": "https://api.www.sbir.gov/public/api/awards",
        },
    )


def downgrade() -> None:
    conn = op.get_bind()
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
