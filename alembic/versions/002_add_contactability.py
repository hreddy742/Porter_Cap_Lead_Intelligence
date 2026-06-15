"""Add contactability_runs and company_contactability tables.

Revision ID: 002_add_contactability
Revises: 001_phase1_initial
Create Date: 2026-06-15

Phase 2A: Contactability-Lite schema foundation.
Adds two tables:
  contactability_runs      — audit log for each enrichment run
  company_contactability   — per-company contactability enrichment results (one row per company)

No Phase 1 tables are altered.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "002_add_contactability"
down_revision = "001_phase1_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── contactability_runs (no FKs to new tables; created first) ─────────────
    op.create_table(
        "contactability_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(20), nullable=False, server_default="running"),
        sa.Column("trigger", sa.String(20), nullable=False, server_default="cli"),
        sa.Column("source_filter", sa.String(20)),
        sa.Column("tier_filter", sa.String(10)),
        sa.Column("company_limit", sa.Integer, nullable=False),
        sa.Column("dry_run", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("companies_attempted", sa.Integer, server_default="0"),
        sa.Column("companies_enriched", sa.Integer, server_default="0"),
        sa.Column("companies_failed", sa.Integer, server_default="0"),
        sa.Column("error_summary", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index("idx_contactability_runs_status", "contactability_runs", ["status"])
    op.create_index("idx_contactability_runs_started_at", "contactability_runs", ["started_at"])

    # ── company_contactability (FKs to companies, lead_candidates, contactability_runs) ──
    op.create_table(
        "company_contactability",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),        # FK added below
        sa.Column("lead_candidate_id", postgresql.UUID(as_uuid=True)),                 # FK added below; nullable
        # Website discovery
        sa.Column("official_website", sa.Text),
        sa.Column("website_confidence", sa.Numeric(3, 2)),
        sa.Column("website_source", sa.String(30)),
        sa.Column("website_source_url", sa.Text),
        # Contact extraction
        sa.Column("contact_page_url", sa.Text),
        sa.Column("phone", sa.String(30)),
        sa.Column("phone_source_url", sa.Text),
        sa.Column("generic_email", sa.String(255)),
        sa.Column("email_source_url", sa.Text),
        sa.Column("address_from_website", sa.Text),
        # SAM.gov entity data
        sa.Column("sam_uei", sa.String(12)),
        sa.Column("sam_match_status", sa.String(20)),
        sa.Column("sam_registration_status", sa.String(30)),
        sa.Column("sam_address", sa.Text),
        # Status rollup
        sa.Column("contactability_status", sa.String(30), nullable=False),
        sa.Column("contactability_score", sa.Integer, server_default="0"),
        sa.Column("contactability_notes", sa.Text),
        # Audit
        sa.Column("enrichment_run_id", postgresql.UUID(as_uuid=True)),                 # FK added below; nullable
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        # Constraints
        sa.CheckConstraint(
            "contactability_status IN ('contactable', 'partially_contactable', 'not_contactable', 'needs_paid_enrichment')",
            name="chk_contactability_status_valid",
        ),
        sa.CheckConstraint(
            "website_confidence IS NULL OR (website_confidence >= 0 AND website_confidence <= 1)",
            name="chk_website_confidence_range",
        ),
        sa.CheckConstraint(
            "contactability_score IS NULL OR (contactability_score >= 0 AND contactability_score <= 10)",
            name="chk_contactability_score_range",
        ),
        sa.UniqueConstraint("company_id", name="uq_company_contactability_company"),
    )
    op.create_index("idx_company_contactability_company_id", "company_contactability", ["company_id"])
    op.create_index("idx_company_contactability_status", "company_contactability", ["contactability_status"])
    op.create_index("idx_company_contactability_last_checked", "company_contactability", ["last_checked_at"])
    op.create_index("idx_company_contactability_sam_uei", "company_contactability", ["sam_uei"])

    # ── FK constraints (added after both tables exist) ────────────────────────
    op.create_foreign_key(
        "fk_company_contactability_company",
        "company_contactability", "companies",
        ["company_id"], ["id"],
    )
    op.create_foreign_key(
        "fk_company_contactability_lead_candidate",
        "company_contactability", "lead_candidates",
        ["lead_candidate_id"], ["id"],
    )
    op.create_foreign_key(
        "fk_company_contactability_run",
        "company_contactability", "contactability_runs",
        ["enrichment_run_id"], ["id"],
    )


def downgrade() -> None:
    # Drop FKs first
    op.drop_constraint("fk_company_contactability_run", "company_contactability", type_="foreignkey")
    op.drop_constraint("fk_company_contactability_lead_candidate", "company_contactability", type_="foreignkey")
    op.drop_constraint("fk_company_contactability_company", "company_contactability", type_="foreignkey")

    # Drop tables in reverse dependency order
    op.drop_table("company_contactability")
    op.drop_table("contactability_runs")
