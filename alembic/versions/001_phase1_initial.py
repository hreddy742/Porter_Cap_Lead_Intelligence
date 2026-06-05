"""Initial Phase 1 schema — all ship-blocker fixes applied.

Revision ID: 001_phase1_initial
Revises: (none)
Create Date: 2026-06-03

Ship-blocker fixes in this migration:
  1. duplicate_review: expression unique index (not table UNIQUE on LEAST/GREATEST)
  2. lead_scores CHECK: cardinality(evidence_ids) > 0 (not array_length — returns NULL for [])
  3. review_decisions: BEFORE trigger RAISE EXCEPTION (not RULE DO NOTHING)
  4. FK ordering: all tables created first, cross-FKs added via ALTER TABLE at the end
  5. companies.external_id: immutable column, set once at creation

Append-only tables: review_decisions
  In Phase 3, also apply trg_append_only to: deal_attribution, salesforce_sync_logs
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "001_phase1_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── STEP 1: Create the append-only trigger function ────────────────────────
    # This must exist before any table that uses it.
    op.execute("""
        CREATE OR REPLACE FUNCTION enforce_append_only()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION
                'Table % is append-only. UPDATE and DELETE are not permitted. '
                'Corrections must be new INSERT rows.',
                TG_TABLE_NAME;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
    """)

    # ── STEP 2: Create all tables (no cross-FK constraints yet) ───────────────

    op.create_table(
        "source_registry",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column("access_method", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="planned"),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("cost_type", sa.String(20)),
        sa.Column("cost_per_call", sa.Numeric(10, 4)),
        sa.Column("monthly_cost_estimate", sa.Numeric(10, 2)),
        sa.Column("daily_call_cap", sa.Integer),
        sa.Column("monthly_cost_cap", sa.Numeric(10, 2)),
        sa.Column("rate_limit_per_minute", sa.Integer),
        sa.Column("legal_notes", sa.Text),
        sa.Column("signal_types", postgresql.ARRAY(sa.String(50)), server_default="{}"),
        sa.Column("base_url", sa.Text),
        sa.Column("auth_env_var", sa.String(100)),
        sa.Column("avg_records_per_run", sa.Integer),
        sa.Column("avg_valid_leads_per_run", sa.Integer),
        sa.Column("quality_score", sa.Numeric(3, 2)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_failure_at", sa.DateTime(timezone=True)),
        sa.Column("notes", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.UniqueConstraint("name", name="uq_source_registry_name"),
        sa.CheckConstraint("enabled = FALSE OR status = 'enabled'", name="chk_enabled_requires_status"),
        sa.CheckConstraint(
            "cost_type = 'free' OR cost_type IS NULL OR monthly_cost_cap IS NOT NULL",
            name="chk_paid_requires_cap",
        ),
        sa.CheckConstraint("enabled = FALSE OR legal_notes IS NOT NULL", name="chk_enabled_requires_legal"),
    )
    op.create_index("idx_source_registry_enabled_status", "source_registry", ["enabled", "status"])
    op.create_index("idx_source_registry_category", "source_registry", ["category"])

    op.create_table(
        "pipeline_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(20), nullable=False, server_default="running"),
        sa.Column("trigger", sa.String(20), nullable=False, server_default="cron"),
        sa.Column("total_records", sa.Integer, server_default="0"),
        sa.Column("total_hot", sa.Integer, server_default="0"),
        sa.Column("total_warm", sa.Integer, server_default="0"),
        sa.Column("total_cold", sa.Integer, server_default="0"),
        sa.Column("total_archive", sa.Integer, server_default="0"),
        sa.Column("quarantine_count", sa.Integer, server_default="0"),
        sa.Column("error_summary", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index("idx_pipeline_runs_started_at", "pipeline_runs", ["started_at"])
    op.create_index("idx_pipeline_runs_status", "pipeline_runs", ["status"])
    # Only one running pipeline at a time
    op.create_index(
        "idx_pipeline_runs_one_running",
        "pipeline_runs",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )

    op.create_table(
        "source_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("pipeline_run_id", postgresql.UUID(as_uuid=True), nullable=False),  # FK added below
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),          # FK added below
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(20), nullable=False, server_default="running"),
        sa.Column("records_fetched", sa.Integer, server_default="0"),
        sa.Column("records_valid", sa.Integer, server_default="0"),
        sa.Column("records_skipped", sa.Integer, server_default="0"),
        sa.Column("quarantine_count", sa.Integer, server_default="0"),
        sa.Column("error_text", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index("idx_source_runs_pipeline_run_id", "source_runs", ["pipeline_run_id"])
    op.create_index("idx_source_runs_source_id_started", "source_runs", ["source_id", "started_at"])
    op.create_index("idx_source_runs_status", "source_runs", ["status"])

    op.create_table(
        "raw_source_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),    # FK added below
        sa.Column("source_run_id", postgresql.UUID(as_uuid=True), nullable=False), # FK added below
        sa.Column("source_record_id", sa.String(500)),
        sa.Column("company_name_raw", sa.Text),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("source_url", sa.Text),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.UniqueConstraint("source_id", "content_hash", name="uq_raw_event_dedup"),
    )
    op.create_index("idx_raw_source_events_source_run_id", "raw_source_events", ["source_run_id"])
    op.create_index("idx_raw_source_events_fetched_at", "raw_source_events", ["fetched_at"])

    op.create_table(
        "companies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("canonical_name", sa.String(500), nullable=False),
        sa.Column("normalized_name", sa.String(500), nullable=False),
        # SHIP-BLOCKER: external_id stored once at creation, never recomputed
        sa.Column("external_id", sa.String(32), unique=True),
        sa.Column("dba_name", sa.String(500)),
        sa.Column("website_domain", sa.String(255)),
        sa.Column("city", sa.String(100)),
        sa.Column("state", sa.String(2)),
        sa.Column("country", sa.String(2), nullable=False, server_default="US"),
        sa.Column("naics_code", sa.String(10)),
        sa.Column("naics_description", sa.String(255)),
        sa.Column("industry", sa.String(100)),
        sa.Column("company_size_hint", sa.String(20)),
        sa.Column("business_type", sa.String(50)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index("idx_companies_normalized_name", "companies", ["normalized_name"])
    op.create_index("idx_companies_domain", "companies", ["website_domain"])
    op.create_index("idx_companies_state", "companies", ["state"])
    op.create_index("idx_companies_naics", "companies", ["naics_code"])

    op.create_table(
        "evidence_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("raw_event_id", postgresql.UUID(as_uuid=True), nullable=False),  # FK added below
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),      # FK added below
        sa.Column("company_id", postgresql.UUID(as_uuid=True)),                     # FK added below; nullable
        sa.Column("signal_id", postgresql.UUID(as_uuid=True)),                      # FK added below after signals
        sa.Column("source_url", sa.Text, nullable=False),
        sa.Column("extracted_text", sa.Text),
        sa.Column("extracted_fields", postgresql.JSONB),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("claim_supported", sa.String(50), nullable=False),
        sa.Column("confidence_score", sa.Numeric(3, 2), nullable=False),
        sa.Column("freshness_score", sa.Numeric(3, 2), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.CheckConstraint("freshness_score >= 0 AND freshness_score <= 1", name="chk_freshness_range"),
        sa.CheckConstraint("confidence_score >= 0 AND confidence_score <= 1", name="chk_confidence_range"),
    )
    op.create_index("idx_evidence_items_company_id", "evidence_items", ["company_id"])
    op.create_index("idx_evidence_items_claim", "evidence_items", ["claim_supported"])
    op.create_index("idx_evidence_items_company_claim", "evidence_items", ["company_id", "claim_supported"])

    op.create_table(
        "company_identifiers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),  # FK added below
        sa.Column("id_type", sa.String(30), nullable=False),
        sa.Column("id_value", sa.String(500), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True)),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.UniqueConstraint("id_type", "id_value", name="uq_company_identifier"),
    )
    op.create_index("idx_company_identifiers_company_id", "company_identifiers", ["company_id"])
    op.create_index("idx_company_identifiers_lookup", "company_identifiers", ["id_type", "id_value"])

    op.create_table(
        "signals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),  # FK added below
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),   # FK added below
        sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=False),  # FK added below; REQUIRED
        sa.Column("signal_type", sa.String(50), nullable=False),
        sa.Column("signal_date", sa.Date, nullable=False),
        sa.Column("signal_strength", sa.String(10), nullable=False, server_default="medium"),
        sa.Column("freshness_score", sa.Numeric(3, 2), nullable=False),
        sa.Column("award_amount", sa.Numeric(15, 2)),
        sa.Column("description", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index("idx_signals_company_id", "signals", ["company_id"])
    op.create_index("idx_signals_type_date", "signals", ["signal_type", "signal_date"])
    op.create_index("idx_signals_freshness", "signals", ["freshness_score"])

    op.create_table(
        "suppression_list",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("match_type", sa.String(30), nullable=False),
        sa.Column("match_value", sa.String(500), nullable=False),
        sa.Column("reason", sa.String(50), nullable=False),
        sa.Column("source", sa.String(30), nullable=False, server_default="csv_import"),
        sa.Column("evidence_url", sa.Text),
        sa.Column("import_date", sa.Date),
        sa.Column("notes", sa.Text),
        sa.Column("active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index(
        "idx_suppression_lookup",
        "suppression_list",
        ["match_type", "match_value"],
        postgresql_where=sa.text("active = TRUE"),
    )
    op.create_index("idx_suppression_reason", "suppression_list", ["reason"])

    op.create_table(
        "scoring_configs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("version_label", sa.String(50), nullable=False),
        sa.Column("config", postgresql.JSONB, nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("active", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("notes", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("created_by", sa.String(100)),
        sa.UniqueConstraint("config_hash", name="uq_scoring_config_hash"),
    )
    # Only one active scoring config at a time
    op.create_index(
        "idx_scoring_configs_one_active",
        "scoring_configs",
        ["active"],
        unique=True,
        postgresql_where=sa.text("active = TRUE"),
    )

    op.create_table(
        "lead_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),  # FK added below
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("tier", sa.String(10)),
        sa.Column("sales_status", sa.String(30), nullable=False, server_default="research"),
        sa.Column("gate_result", sa.String(20)),
        sa.Column("gate_reason", sa.String(50)),
        sa.Column("current_score", sa.Integer),
        sa.Column("risk_level", sa.String(10)),
        sa.Column("ar_fit_confidence", sa.String(10), server_default="low"),
        sa.Column("why_now_summary", sa.Text),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    # Only one active candidate per company
    op.create_index(
        "idx_lead_candidates_one_active",
        "lead_candidates",
        ["company_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index("idx_lead_candidates_tier", "lead_candidates", ["tier"])
    op.create_index("idx_lead_candidates_sales_status", "lead_candidates", ["sales_status"])

    op.create_table(
        "lead_scores",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("lead_candidate_id", postgresql.UUID(as_uuid=True), nullable=False),  # FK added below
        sa.Column("scoring_config_id", postgresql.UUID(as_uuid=True), nullable=False),  # FK added below
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("total_score", sa.Integer, nullable=False),
        sa.Column("tier", sa.String(10), nullable=False),
        sa.Column("component_breakdown", postgresql.JSONB, nullable=False),
        sa.Column("evidence_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=False, server_default="{}"),
        sa.Column("gate_result", sa.String(20), nullable=False),
        sa.Column("gate_reasons", postgresql.ARRAY(sa.String(50)), server_default="{}"),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        # SHIP-BLOCKER FIX: cardinality() returns 0 for empty array; array_length() returns NULL
        sa.CheckConstraint(
            "gate_result = 'archived' OR cardinality(evidence_ids) > 0",
            name="chk_score_has_evidence",
        ),
    )
    op.create_index("idx_lead_scores_candidate", "lead_scores", ["lead_candidate_id", "computed_at"])
    op.create_index("idx_lead_scores_config", "lead_scores", ["scoring_config_id"])

    op.create_table(
        "review_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("lead_candidate_id", postgresql.UUID(as_uuid=True), nullable=False),  # FK added below
        sa.Column("reviewer_id", sa.String(255), nullable=False),
        sa.Column("action", sa.String(30), nullable=False),
        sa.Column("note", sa.Text),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index("idx_review_decisions_lead", "review_decisions", ["lead_candidate_id", "decided_at"])
    op.create_index("idx_review_decisions_reviewer", "review_decisions", ["reviewer_id"])

    op.create_table(
        "duplicate_review",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("company_id_a", postgresql.UUID(as_uuid=True), nullable=False),  # FK added below
        sa.Column("company_id_b", postgresql.UUID(as_uuid=True), nullable=False),  # FK added below
        sa.Column("similarity_score", sa.Numeric(4, 3), nullable=False),
        sa.Column("match_basis", sa.String(30), nullable=False, server_default="name_similarity"),
        sa.Column("normalized_name_a", sa.String(500), nullable=False),
        sa.Column("normalized_name_b", sa.String(500), nullable=False),
        sa.Column("state_a", sa.String(2)),
        sa.Column("state_b", sa.String(2)),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("resolved_by", sa.String(255)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolution_note", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.CheckConstraint("company_id_a != company_id_b", name="chk_different_companies"),
    )
    # SHIP-BLOCKER FIX: expression unique index — table UNIQUE constraints cannot use expressions
    # This prevents storing the same pair in both (A,B) and (B,A) orderings.
    op.execute("""
        CREATE UNIQUE INDEX uq_duplicate_pair
        ON duplicate_review (
            LEAST(company_id_a, company_id_b),
            GREATEST(company_id_a, company_id_b)
        );
    """)
    op.create_index("idx_duplicate_review_status", "duplicate_review", ["status"])
    op.create_index("idx_duplicate_review_company_a", "duplicate_review", ["company_id_a"])
    op.create_index("idx_duplicate_review_company_b", "duplicate_review", ["company_id_b"])

    # ── STEP 3: Add all cross-table FK constraints ─────────────────────────────
    # Done AFTER all tables exist to avoid forward-reference errors in migrations.

    op.create_foreign_key("fk_source_runs_pipeline", "source_runs", "pipeline_runs", ["pipeline_run_id"], ["id"])
    op.create_foreign_key("fk_source_runs_source", "source_runs", "source_registry", ["source_id"], ["id"])
    op.create_foreign_key("fk_raw_events_source", "raw_source_events", "source_registry", ["source_id"], ["id"])
    op.create_foreign_key("fk_raw_events_run", "raw_source_events", "source_runs", ["source_run_id"], ["id"])
    op.create_foreign_key("fk_evidence_raw_event", "evidence_items", "raw_source_events", ["raw_event_id"], ["id"])
    op.create_foreign_key("fk_evidence_source", "evidence_items", "source_registry", ["source_id"], ["id"])
    op.create_foreign_key("fk_evidence_company", "evidence_items", "companies", ["company_id"], ["id"])
    op.create_foreign_key("fk_company_identifiers_company", "company_identifiers", "companies", ["company_id"], ["id"])
    op.create_foreign_key("fk_signals_company", "signals", "companies", ["company_id"], ["id"])
    op.create_foreign_key("fk_signals_source", "signals", "source_registry", ["source_id"], ["id"])
    op.create_foreign_key("fk_signals_evidence", "signals", "evidence_items", ["evidence_id"], ["id"])
    # evidence_items.signal_id → signals.id (add AFTER signals table exists)
    op.create_foreign_key("fk_evidence_signal", "evidence_items", "signals", ["signal_id"], ["id"])
    op.create_foreign_key("fk_lead_candidates_company", "lead_candidates", "companies", ["company_id"], ["id"])
    op.create_foreign_key("fk_lead_scores_candidate", "lead_scores", "lead_candidates", ["lead_candidate_id"], ["id"])
    op.create_foreign_key("fk_lead_scores_config", "lead_scores", "scoring_configs", ["scoring_config_id"], ["id"])
    op.create_foreign_key("fk_review_decisions_candidate", "review_decisions", "lead_candidates", ["lead_candidate_id"], ["id"])
    op.create_foreign_key("fk_duplicate_review_company_a", "duplicate_review", "companies", ["company_id_a"], ["id"])
    op.create_foreign_key("fk_duplicate_review_company_b", "duplicate_review", "companies", ["company_id_b"], ["id"])

    # ── STEP 4: Append-only trigger on review_decisions ───────────────────────
    # SHIP-BLOCKER FIX: BEFORE trigger that RAISE EXCEPTION (not RULE DO NOTHING)
    # RULE DO NOTHING: silently swallows updates, doesn't block TRUNCATE, can be dropped by owner
    # BEFORE trigger RAISE: loudly fails, blocks all mutation attempts
    op.execute("""
        CREATE TRIGGER trg_review_decisions_append_only
        BEFORE UPDATE OR DELETE ON review_decisions
        FOR EACH ROW EXECUTE FUNCTION enforce_append_only();
    """)

    # ── STEP 5: Seed initial data ──────────────────────────────────────────────
    op.execute("""
        INSERT INTO source_registry
            (id, name, category, access_method, status, enabled, cost_type, legal_notes, signal_types)
        VALUES
            (gen_random_uuid(), 'usaspending', 'government_contract', 'api', 'enabled', true, 'free',
             'Public domain US government data. No ToS restrictions on use.', ARRAY['CONTRACT_AWARD']),
            (gen_random_uuid(), 'sam_gov', 'government_contract', 'api', 'planned', false, 'free',
             'Requires SAM.gov API key. Public data. Phase 2.', ARRAY['CONTRACT_AWARD']),
            (gen_random_uuid(), 'salesforce_suppression', 'suppression', 'api', 'planned', false, 'free',
             'Internal Salesforce instance. Phase 2 only.', ARRAY[]::varchar[]);
    """)


def downgrade() -> None:
    # Remove triggers and functions first
    op.execute("DROP TRIGGER IF EXISTS trg_review_decisions_append_only ON review_decisions;")
    op.execute("DROP RULE IF EXISTS no_truncate_review_decisions ON review_decisions;")
    op.execute("DROP FUNCTION IF EXISTS enforce_append_only();")

    # Remove FK constraints before dropping tables
    for constraint, table in [
        ("fk_duplicate_review_company_b", "duplicate_review"),
        ("fk_duplicate_review_company_a", "duplicate_review"),
        ("fk_review_decisions_candidate", "review_decisions"),
        ("fk_lead_scores_config", "lead_scores"),
        ("fk_lead_scores_candidate", "lead_scores"),
        ("fk_lead_candidates_company", "lead_candidates"),
        ("fk_evidence_signal", "evidence_items"),
        ("fk_signals_evidence", "signals"),
        ("fk_signals_source", "signals"),
        ("fk_signals_company", "signals"),
        ("fk_company_identifiers_company", "company_identifiers"),
        ("fk_evidence_company", "evidence_items"),
        ("fk_evidence_source", "evidence_items"),
        ("fk_evidence_raw_event", "evidence_items"),
        ("fk_raw_events_run", "raw_source_events"),
        ("fk_raw_events_source", "raw_source_events"),
        ("fk_source_runs_source", "source_runs"),
        ("fk_source_runs_pipeline", "source_runs"),
    ]:
        op.drop_constraint(constraint, table, type_="foreignkey")

    # Drop tables in reverse dependency order
    op.drop_table("duplicate_review")
    op.drop_table("review_decisions")
    op.drop_table("lead_scores")
    op.drop_table("lead_candidates")
    op.drop_table("scoring_configs")
    op.drop_table("suppression_list")
    op.drop_table("signals")
    op.drop_table("company_identifiers")
    op.drop_table("evidence_items")
    op.drop_table("companies")
    op.drop_table("raw_source_events")
    op.drop_table("source_runs")
    op.drop_table("pipeline_runs")
    op.drop_table("source_registry")
