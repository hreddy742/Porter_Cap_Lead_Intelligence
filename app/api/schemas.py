"""
Pydantic v2 response schemas for the Porter Capital Lead Intelligence API.
Read-only — no write schemas exist in Stage 2A.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class RunContextSchema(BaseModel):
    has_runs: bool
    pipeline_run_id: str | None
    status: str | None
    started_at: str | None
    finished_at: str | None
    records_fetched: int | None
    raw_events_stored: int | None
    leads_scored: int | None
    errors: list[dict[str, Any]]


class TierCountsSchema(BaseModel):
    hot: int
    warm: int
    cold: int
    archive: int
    total: int
    pending_review: int


class DashboardSummarySchema(BaseModel):
    run_context: RunContextSchema
    tier_counts: TierCountsSchema
    warning: str


class LeadListItemSchema(BaseModel):
    lead_id: str
    company_id: str
    company_name: str
    tier: str | None
    score: int | None
    sales_status: str
    primary_source: str | None
    signal_type: str | None = None
    latest_signal_date: str | None
    max_award_amount: str | None
    is_new_in_run: bool
    created_at: str
    updated_at: str
    sector_excluded: bool = False
    sector_excluded_reason: str | None = None


class LeadsListResponse(BaseModel):
    items: list[LeadListItemSchema]
    total: int
    limit: int
    offset: int
    warning: str


class EvidenceItemSchema(BaseModel):
    id: str
    claim_supported: str
    confidence_score: float
    source_url: str | None
    captured_at: str
    extracted_fields: dict[str, Any] | None


class SignalSchema(BaseModel):
    id: str
    signal_type: str
    signal_date: str
    signal_strength: str
    award_amount: str | None


class ReviewDecisionSchema(BaseModel):
    id: str
    action: str
    reviewer_id: str
    decided_at: str
    note: str | None


class ContactabilitySchema(BaseModel):
    contactability_status: str
    contactability_score: int | None
    sam_match_status: str | None
    sam_uei: str | None
    sam_registration_status: str | None
    official_website: str | None
    phone: str | None
    generic_email: str | None
    last_checked_at: str
    sam_note: str


class ScoreDetailSchema(BaseModel):
    total_score: int
    tier: str
    gate_result: str
    component_breakdown: dict[str, Any]
    config_hash: str


class LeadDetailSchema(BaseModel):
    lead_id: str
    company_id: str
    company_name: str
    company_state: str | None
    company_naics: str | None
    company_naics_description: str | None
    company_domain: str | None
    company_industry: str | None
    company_business_type: str | None
    tier: str | None
    score: int | None
    sales_status: str
    gate_result: str | None
    gate_reason: str | None
    ar_fit_confidence: str | None
    why_now_summary: str | None
    latest_score: ScoreDetailSchema | None
    evidence: list[EvidenceItemSchema]
    signals: list[SignalSchema]
    review_history: list[ReviewDecisionSchema]
    contactability: ContactabilitySchema | None
    warning: str
    contactability_note: str
