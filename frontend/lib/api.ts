/**
 * Thin API client for the Porter Capital Lead Intelligence FastAPI backend.
 * All functions are server-side only (process.env.API_URL is not public).
 */

const API_URL = process.env.API_URL ?? "http://localhost:8000";

// ── Response types ────────────────────────────────────────────────────────────

export interface RunContext {
  has_runs: boolean;
  pipeline_run_id: string | null;
  status: string | null;
  started_at: string | null;
  finished_at: string | null;
  records_fetched: number | null;
  raw_events_stored: number | null;
  leads_scored: number | null;
  errors: { source_run_id: string | null; error_text: string | null }[];
}

export interface TierCounts {
  hot: number;
  warm: number;
  cold: number;
  archive: number;
  total: number;
  pending_review: number;
}

export interface DashboardSummary {
  run_context: RunContext;
  tier_counts: TierCounts;
  warning: string;
}

export interface LeadListItem {
  lead_id: string;
  company_id: string;
  company_name: string;
  tier: string | null;
  score: number | null;
  sales_status: string;
  primary_source: string | null;
  signal_type: string | null;
  latest_signal_date: string | null;
  max_award_amount: string | null;
  is_new_in_run: boolean;
  created_at: string;
  updated_at: string;
  sector_excluded: boolean;
  sector_excluded_reason: string | null;
  // Optional fields that may be returned by the API
  company_naics?: string | null;
  company_naics_description?: string | null;
  company_industry?: string | null;
  awarding_agency?: string | null;
  company_state?: string | null;
}

export interface LeadsListResponse {
  items: LeadListItem[];
  total: number;
  limit: number;
  offset: number;
  warning: string;
}

export interface EvidenceItem {
  id: string;
  claim_supported: string;
  confidence_score: number;
  source_url: string | null;
  captured_at: string;
  extracted_fields: Record<string, unknown> | null;
}

export interface Signal {
  id: string;
  signal_type: string;
  signal_date: string;
  signal_strength: string;
  award_amount: string | null;
}

export interface ReviewDecision {
  id: string;
  action: string;
  reviewer_id: string;
  decided_at: string;
  note: string | null;
}

export interface Contactability {
  contactability_status: string;
  contactability_score: number | null;
  sam_match_status: string | null;
  sam_uei: string | null;
  sam_registration_status: string | null;
  official_website: string | null;
  phone: string | null;
  generic_email: string | null;
  last_checked_at: string;
  sam_note: string;
}

export interface ScoreDetail {
  total_score: number;
  tier: string;
  gate_result: string;
  component_breakdown: Record<string, unknown>;
  config_hash: string;
}

export interface LeadDetail {
  lead_id: string;
  company_id: string;
  company_name: string;
  company_state: string | null;
  company_naics: string | null;
  company_naics_description: string | null;
  company_domain: string | null;
  company_industry: string | null;
  company_business_type: string | null;
  tier: string | null;
  score: number | null;
  sales_status: string;
  gate_result: string | null;
  gate_reason: string | null;
  ar_fit_confidence: string | null;
  why_now_summary: string | null;
  latest_score: ScoreDetail | null;
  evidence: EvidenceItem[];
  signals: Signal[];
  review_history: ReviewDecision[];
  contactability: Contactability | null;
  warning: string;
  contactability_note: string;
}

// ── Fetch helpers ─────────────────────────────────────────────────────────────

export async function fetchSummary(): Promise<DashboardSummary> {
  const res = await fetch(`${API_URL}/api/dashboard/summary`, {
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`Dashboard summary failed: ${res.status}`);
  return res.json();
}

export async function fetchLeads(
  params: Record<string, string> = {}
): Promise<LeadsListResponse> {
  const qs = new URLSearchParams(params).toString();
  const url = `${API_URL}/api/leads${qs ? `?${qs}` : ""}`;
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`Leads list failed: ${res.status}`);
  return res.json();
}

export async function fetchLead(
  id: string
): Promise<LeadDetail | null> {
  const res = await fetch(`${API_URL}/api/leads/${id}`, { cache: "no-store" });
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`Lead detail failed: ${res.status}`);
  return res.json();
}
