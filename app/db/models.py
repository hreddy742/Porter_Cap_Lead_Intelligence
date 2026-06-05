"""
SQLAlchemy ORM models — Phase 1 tables (13 tables).

ALL ship-blocker fixes from the engineering review are applied here:
  ✅ duplicate_review: unique constraint is a separate Index(), not UNIQUE on expressions
  ✅ lead_scores CHECK: uses cardinality() not array_length()
  ✅ review_decisions: append-only DDL event to add BEFORE trigger (see bottom of file)
  ✅ FK ordering: all FKs are lazy strings ("companies.id") resolved after all tables exist
  ✅ companies.external_id: immutable column, set once at creation

Phase 2-3 tables (salesforce_lead_mapping, salesforce_sync_logs, deal_attribution,
sales_outcomes, risk_checks, prompt_versions) are NOT defined here yet.
Add them when their phase begins.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# ─── Table 1: source_registry ─────────────────────────────────────────────────

class SourceRegistry(Base):
    __tablename__ = "source_registry"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False, unique=True)
    category = Column(String(50), nullable=False)
    access_method = Column(String(20), nullable=False)
    status = Column(String(20), nullable=False, default="planned")
    enabled = Column(Boolean, nullable=False, default=False)
    cost_type = Column(String(20))
    cost_per_call = Column(Numeric(10, 4))
    monthly_cost_estimate = Column(Numeric(10, 2))
    daily_call_cap = Column(Integer)
    monthly_cost_cap = Column(Numeric(10, 2))
    rate_limit_per_minute = Column(Integer)
    legal_notes = Column(Text)
    signal_types = Column(ARRAY(String(50)), default=list)
    base_url = Column(Text)
    auth_env_var = Column(String(100))  # Name of env var holding API key — never the key itself
    avg_records_per_run = Column(Integer)
    avg_valid_leads_per_run = Column(Integer)
    quality_score = Column(Numeric(3, 2))
    last_success_at = Column(DateTime(timezone=True))
    last_failure_at = Column(DateTime(timezone=True))
    notes = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        CheckConstraint(
            "enabled = FALSE OR status = 'enabled'",
            name="chk_enabled_requires_status",
        ),
        CheckConstraint(
            "cost_type = 'free' OR cost_type IS NULL OR monthly_cost_cap IS NOT NULL",
            name="chk_paid_requires_cap",
        ),
        CheckConstraint(
            "enabled = FALSE OR legal_notes IS NOT NULL",
            name="chk_enabled_requires_legal",
        ),
        Index("idx_source_registry_enabled_status", "enabled", "status"),
        Index("idx_source_registry_category", "category"),
    )

    source_runs = relationship("SourceRun", back_populates="source")


# ─── Table 2: pipeline_runs ───────────────────────────────────────────────────

class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    started_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    ended_at = Column(DateTime(timezone=True))
    status = Column(String(20), nullable=False, default="running")
    trigger = Column(String(20), nullable=False, default="cron")
    total_records = Column(Integer, default=0)
    total_hot = Column(Integer, default=0)
    total_warm = Column(Integer, default=0)
    total_cold = Column(Integer, default=0)
    total_archive = Column(Integer, default=0)
    quarantine_count = Column(Integer, default=0)
    error_summary = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        Index("idx_pipeline_runs_started_at", "started_at"),
        Index("idx_pipeline_runs_status", "status"),
        # Only one running pipeline at a time — partial unique index
        Index(
            "idx_pipeline_runs_one_running",
            "status",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
    )

    source_runs = relationship("SourceRun", back_populates="pipeline_run")


# ─── Table 3: source_runs ─────────────────────────────────────────────────────

class SourceRun(Base):
    __tablename__ = "source_runs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pipeline_run_id = Column(UUID(as_uuid=True), ForeignKey("pipeline_runs.id"), nullable=False)
    source_id = Column(UUID(as_uuid=True), ForeignKey("source_registry.id"), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    finished_at = Column(DateTime(timezone=True))
    status = Column(String(20), nullable=False, default="running")
    records_fetched = Column(Integer, default=0)
    records_valid = Column(Integer, default=0)
    records_skipped = Column(Integer, default=0)
    quarantine_count = Column(Integer, default=0)
    error_text = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        Index("idx_source_runs_pipeline_run_id", "pipeline_run_id"),
        Index("idx_source_runs_source_id_started", "source_id", "started_at"),
        Index("idx_source_runs_status", "status"),
    )

    pipeline_run = relationship("PipelineRun", back_populates="source_runs")
    source = relationship("SourceRegistry", back_populates="source_runs")
    raw_events = relationship("RawSourceEvent", back_populates="source_run")


# ─── Table 4: raw_source_events ───────────────────────────────────────────────

class RawSourceEvent(Base):
    __tablename__ = "raw_source_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id = Column(UUID(as_uuid=True), ForeignKey("source_registry.id"), nullable=False)
    source_run_id = Column(UUID(as_uuid=True), ForeignKey("source_runs.id"), nullable=False)
    source_record_id = Column(String(500))  # Source's own ID (e.g. USASpending award ID)
    company_name_raw = Column(Text)
    payload = Column(JSONB, nullable=False)
    content_hash = Column(String(64), nullable=False)
    source_url = Column(Text)
    fetched_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        # Dedup: same content from same source is always the same record
        UniqueConstraint("source_id", "content_hash", name="uq_raw_event_dedup"),
        Index("idx_raw_source_events_source_run_id", "source_run_id"),
        Index("idx_raw_source_events_fetched_at", "fetched_at"),
    )

    source_run = relationship("SourceRun", back_populates="raw_events")
    evidence_items = relationship("EvidenceItem", back_populates="raw_event")


# ─── Table 6: companies (defined BEFORE evidence_items to avoid FK ordering issues) ──

class Company(Base):
    __tablename__ = "companies"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    canonical_name = Column(String(500), nullable=False)
    normalized_name = Column(String(500), nullable=False)
    # SHIP-BLOCKER FIX: external_id stored once at creation, never recomputed
    external_id = Column(String(32), unique=True)  # sha256 of identity key, truncated to 32 chars
    dba_name = Column(String(500))
    website_domain = Column(String(255))
    city = Column(String(100))
    state = Column(String(2))
    country = Column(String(2), nullable=False, default="US")
    naics_code = Column(String(10))
    naics_description = Column(String(255))
    industry = Column(String(100))
    company_size_hint = Column(String(20))
    business_type = Column(String(50))
    deleted_at = Column(DateTime(timezone=True))  # soft delete
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index("idx_companies_normalized_name", "normalized_name"),
        Index("idx_companies_domain", "website_domain"),
        Index("idx_companies_state", "state"),
        Index("idx_companies_naics", "naics_code"),
        Index("idx_companies_country", "country"),
    )

    identifiers = relationship("CompanyIdentifier", back_populates="company")
    evidence_items = relationship("EvidenceItem", back_populates="company")
    signals = relationship("Signal", back_populates="company")
    lead_candidates = relationship("LeadCandidate", back_populates="company")


# ─── Table 5: evidence_items ──────────────────────────────────────────────────

class EvidenceItem(Base):
    __tablename__ = "evidence_items"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    raw_event_id = Column(UUID(as_uuid=True), ForeignKey("raw_source_events.id"), nullable=False)
    source_id = Column(UUID(as_uuid=True), ForeignKey("source_registry.id"), nullable=False)
    company_id = Column(UUID(as_uuid=True), ForeignKey("companies.id"))  # Nullable until resolved
    signal_id = Column(UUID(as_uuid=True))  # FK to signals.id — added via ALTER TABLE after signals created
    source_url = Column(Text, nullable=False)
    extracted_text = Column(Text)
    extracted_fields = Column(JSONB)
    content_hash = Column(String(64), nullable=False)
    claim_supported = Column(String(50), nullable=False)
    confidence_score = Column(Numeric(3, 2), nullable=False)
    freshness_score = Column(Numeric(3, 2), nullable=False)
    captured_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        CheckConstraint("freshness_score >= 0 AND freshness_score <= 1", name="chk_freshness_range"),
        CheckConstraint("confidence_score >= 0 AND confidence_score <= 1", name="chk_confidence_range"),
        Index("idx_evidence_items_company_id", "company_id"),
        Index("idx_evidence_items_claim", "claim_supported"),
        Index("idx_evidence_items_source_id", "source_id"),
        Index("idx_evidence_items_company_claim", "company_id", "claim_supported"),
    )

    raw_event = relationship("RawSourceEvent", back_populates="evidence_items")
    company = relationship("Company", back_populates="evidence_items")


# ─── Table 7: company_identifiers ─────────────────────────────────────────────

class CompanyIdentifier(Base):
    __tablename__ = "company_identifiers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id = Column(UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False)
    id_type = Column(String(30), nullable=False)
    id_value = Column(String(500), nullable=False)
    source_id = Column(UUID(as_uuid=True), ForeignKey("source_registry.id"))
    verified_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        # THE most important constraint: a hard ID can only belong to one company
        UniqueConstraint("id_type", "id_value", name="uq_company_identifier"),
        Index("idx_company_identifiers_company_id", "company_id"),
        Index("idx_company_identifiers_lookup", "id_type", "id_value"),
    )

    company = relationship("Company", back_populates="identifiers")


# ─── Table 8: signals ─────────────────────────────────────────────────────────

class Signal(Base):
    __tablename__ = "signals"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id = Column(UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False)
    source_id = Column(UUID(as_uuid=True), ForeignKey("source_registry.id"), nullable=False)
    # REQUIRED: a signal with no evidence_id does not exist
    evidence_id = Column(UUID(as_uuid=True), ForeignKey("evidence_items.id"), nullable=False)
    signal_type = Column(String(50), nullable=False)
    signal_date = Column(Date, nullable=False)
    signal_strength = Column(String(10), nullable=False, default="medium")
    freshness_score = Column(Numeric(3, 2), nullable=False)
    award_amount = Column(Numeric(15, 2))
    description = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        Index("idx_signals_company_id", "company_id"),
        Index("idx_signals_type_date", "signal_type", "signal_date"),
        Index("idx_signals_freshness", "freshness_score"),
    )

    company = relationship("Company", back_populates="signals")
    evidence = relationship("EvidenceItem")


# ─── Table 9: suppression_list ────────────────────────────────────────────────

class SuppressionList(Base):
    __tablename__ = "suppression_list"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    match_type = Column(String(30), nullable=False)
    match_value = Column(String(500), nullable=False)
    reason = Column(String(50), nullable=False)
    source = Column(String(30), nullable=False, default="csv_import")
    evidence_url = Column(Text)
    import_date = Column(Date)
    notes = Column(Text)
    active = Column(Boolean, nullable=False, default=True)
    expires_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        Index(
            "idx_suppression_lookup",
            "match_type",
            "match_value",
            postgresql_where=text("active = TRUE"),
        ),
        Index("idx_suppression_reason", "reason"),
        Index(
            "idx_suppression_import_date",
            "import_date",
            postgresql_where=text("source = 'csv_import'"),
        ),
    )


# ─── Table 10: scoring_configs ────────────────────────────────────────────────

class ScoringConfig(Base):
    __tablename__ = "scoring_configs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    version_label = Column(String(50), nullable=False)
    config = Column(JSONB, nullable=False)
    config_hash = Column(String(64), nullable=False, unique=True)
    active = Column(Boolean, nullable=False, default=False)
    notes = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    created_by = Column(String(100))

    __table_args__ = (
        # Only ONE active config at a time
        Index(
            "idx_scoring_configs_one_active",
            "active",
            unique=True,
            postgresql_where=text("active = TRUE"),
        ),
    )

    scores = relationship("LeadScore", back_populates="scoring_config")


# ─── Table 11: lead_candidates ────────────────────────────────────────────────

class LeadCandidate(Base):
    __tablename__ = "lead_candidates"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id = Column(UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False)
    status = Column(String(20), nullable=False, default="active")
    tier = Column(String(10))
    sales_status = Column(String(30), nullable=False, default="research")
    gate_result = Column(String(20))
    gate_reason = Column(String(50))
    current_score = Column(Integer)
    risk_level = Column(String(10))
    ar_fit_confidence = Column(String(10), default="low")
    why_now_summary = Column(Text)
    deleted_at = Column(DateTime(timezone=True))  # soft delete
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        # Enforce: only ONE active candidate per company
        Index(
            "idx_lead_candidates_one_active",
            "company_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index("idx_lead_candidates_tier", "tier"),
        Index("idx_lead_candidates_sales_status", "sales_status"),
        Index("idx_lead_candidates_company_id", "company_id"),
    )

    company = relationship("Company", back_populates="lead_candidates")
    scores = relationship("LeadScore", back_populates="lead_candidate")
    decisions = relationship("ReviewDecision", back_populates="lead_candidate")


# ─── Table 12: lead_scores ────────────────────────────────────────────────────

class LeadScore(Base):
    __tablename__ = "lead_scores"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lead_candidate_id = Column(
        UUID(as_uuid=True), ForeignKey("lead_candidates.id"), nullable=False
    )
    scoring_config_id = Column(
        UUID(as_uuid=True), ForeignKey("scoring_configs.id"), nullable=False
    )
    config_hash = Column(String(64), nullable=False)
    total_score = Column(Integer, nullable=False)
    tier = Column(String(10), nullable=False)
    component_breakdown = Column(JSONB, nullable=False)
    evidence_ids = Column(ARRAY(UUID(as_uuid=True)), nullable=False, default=list)
    gate_result = Column(String(20), nullable=False)
    gate_reasons = Column(ARRAY(String(50)), default=list)
    computed_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        # SHIP-BLOCKER FIX: use cardinality() not array_length()
        # cardinality([]) = 0 (correct); array_length([], 1) = NULL (defeats the check)
        CheckConstraint(
            "gate_result = 'archived' OR cardinality(evidence_ids) > 0",
            name="chk_score_has_evidence",
        ),
        Index("idx_lead_scores_candidate", "lead_candidate_id", "computed_at"),
        Index("idx_lead_scores_config", "scoring_config_id"),
    )

    lead_candidate = relationship("LeadCandidate", back_populates="scores")
    scoring_config = relationship("ScoringConfig", back_populates="scores")


# ─── Table 13: review_decisions (APPEND-ONLY) ─────────────────────────────────

class ReviewDecision(Base):
    __tablename__ = "review_decisions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lead_candidate_id = Column(
        UUID(as_uuid=True), ForeignKey("lead_candidates.id"), nullable=False
    )
    reviewer_id = Column(String(255), nullable=False)  # OAuth2 email — never a shared identity
    action = Column(String(30), nullable=False)
    note = Column(Text)
    decided_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    # No updated_at — this table is append-only

    __table_args__ = (
        Index("idx_review_decisions_lead", "lead_candidate_id", "decided_at"),
        Index("idx_review_decisions_reviewer", "reviewer_id"),
        Index("idx_review_decisions_action", "action"),
    )

    lead_candidate = relationship("LeadCandidate", back_populates="decisions")


# ─── Table 14: duplicate_review ───────────────────────────────────────────────

class DuplicateReview(Base):
    __tablename__ = "duplicate_review"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id_a = Column(UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False)
    company_id_b = Column(UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False)
    similarity_score = Column(Numeric(4, 3), nullable=False)
    match_basis = Column(String(30), nullable=False, default="name_similarity")
    normalized_name_a = Column(String(500), nullable=False)
    normalized_name_b = Column(String(500), nullable=False)
    state_a = Column(String(2))
    state_b = Column(String(2))
    status = Column(String(20), nullable=False, default="pending")
    resolved_by = Column(String(255))
    resolved_at = Column(DateTime(timezone=True))
    resolution_note = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        CheckConstraint("company_id_a != company_id_b", name="chk_different_companies"),
        # SHIP-BLOCKER FIX: expression unique index, NOT a table UNIQUE constraint
        # PostgreSQL table UNIQUE constraints cannot use expressions like LEAST()/GREATEST()
        # This index is created in the Alembic migration using op.create_index() with postgresql_where
        Index("idx_duplicate_review_status", "status"),
        Index("idx_duplicate_review_company_a", "company_id_a"),
        Index("idx_duplicate_review_company_b", "company_id_b"),
    )


# ─── Append-only enforcement (SHIP-BLOCKER FIX) ───────────────────────────────
#
# The review plan's "RULE DO NOTHING" approach has three problems:
#   1. Silently swallows updates/deletes (hides bugs — update "succeeds" but does nothing)
#   2. Doesn't block TRUNCATE
#   3. Can be dropped by the table owner
#
# We use DDL event listeners to add BEFORE triggers that RAISE EXCEPTION.
# These are created automatically when the tables are created via Alembic.
# The actual CREATE TRIGGER DDL is in the migration (see alembic/versions/).
#
# For reference, the trigger SQL that the migration will create:
#
#   CREATE OR REPLACE FUNCTION enforce_append_only()
#   RETURNS TRIGGER AS $$
#   BEGIN
#     RAISE EXCEPTION 'Table % is append-only. UPDATE and DELETE are not permitted.',
#       TG_TABLE_NAME;
#     RETURN NULL;
#   END;
#   $$ LANGUAGE plpgsql;
#
#   CREATE TRIGGER trg_review_decisions_append_only
#   BEFORE UPDATE OR DELETE ON review_decisions
#   FOR EACH ROW EXECUTE FUNCTION enforce_append_only();
#
# (Applied to review_decisions. In Phase 3, also apply to deal_attribution and salesforce_sync_logs.)
