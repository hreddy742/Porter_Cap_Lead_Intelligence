"""Seed usaspending_subawards source in source_registry.

Revision ID: 004_seed_usaspending_subawards_source
Revises: 003_seed_default_scoring_config

Adds the USASpending subawards connector as Porter's second lead source.
enabled=False by default — enable only after manual UAT of 20+ leads.

Source notes:
  - /api/v2/subawards/ returns 6 fields: id, subaward_number, description,
    action_date, amount, recipient_name
  - No UEI, no NAICS, no state, no prime contractor in response
  - API filters are silently ignored (verified June 2026); connector sorts
    by id desc and does not filter server-side
  - Amount cap ($1B) and date year guard (2000-2030) applied in Pydantic validator
  - Company resolution is name-only; every attempt logs a warning
"""

import uuid as uuid_module

from alembic import op
import sqlalchemy as sa

revision = "004_subawards_source_seed"
down_revision = "003_seed_default_scoring_config"
branch_labels = None
depends_on = None

_SOURCE_ID = uuid_module.UUID("00000000-0000-0000-0000-000000000002")


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
                ARRAY['SUBCONTRACT_AWARD']::varchar[],
                :base_url,
                NOW(),
                NOW()
            WHERE NOT EXISTS (
                SELECT 1 FROM source_registry WHERE name = :name
            )
        """),
        {
            "id": str(_SOURCE_ID),
            "name": "usaspending_subawards",
            "category": "government_contract",
            "access_method": "api",
            "status": "planned",
            "enabled": False,
            "cost_type": "free",
            "legal_notes": (
                "Public domain US government data (FFATA subcontract disclosures). "
                "No ToS restrictions on use. "
                "Enable only after manual UAT: review 20+ leads and confirm signal quality."
            ),
            "base_url": "https://api.usaspending.gov/api/v2/subawards/",
        },
    )


def downgrade() -> None:
    conn = op.get_bind()
    # Must cascade: signals → evidence_items → raw_source_events → source_runs →
    # company_identifiers → source_registry
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
