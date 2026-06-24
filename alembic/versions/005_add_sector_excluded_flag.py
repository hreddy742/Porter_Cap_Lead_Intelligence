"""Add sector_excluded flag columns to lead_candidates.

Revision ID: 005_add_sector_excluded_flag
Revises: 004_subawards_source_seed
Create Date: 2026-06-24

Phase 2B Feature 1: soft-flag leads whose NAICS falls in a sector Porter
does not currently focus on per ICP policy (confirmed by John Cox Miller,
June 24 2026).

Excluded sectors: 11 Agriculture, 22 Utilities, 23 Construction,
  52 Finance and Insurance, 61 Educational Services, 62 Health Care,
  71 Arts and Entertainment, 92 Public Administration.

Two new columns on lead_candidates:
  sector_excluded        — Boolean, server_default=false, non-nullable
  sector_excluded_reason — String(200), nullable

Leads are NOT hard-blocked. They are scored and stored normally.
sector_excluded=True means the lead is hidden from sales by default and
available via toggle for researchers and Phase 4 AI review.
"""

from alembic import op
import sqlalchemy as sa

revision = "005_add_sector_excluded_flag"
down_revision = "004_subawards_source_seed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "lead_candidates",
        sa.Column(
            "sector_excluded",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "lead_candidates",
        sa.Column("sector_excluded_reason", sa.String(200), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("lead_candidates", "sector_excluded_reason")
    op.drop_column("lead_candidates", "sector_excluded")
