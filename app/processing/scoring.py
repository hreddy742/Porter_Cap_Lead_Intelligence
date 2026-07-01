"""
Scoring engine — Phase 1.

score_company: run mandatory gates, load the active scoring config, compute
six component scores, persist lead_candidate + lead_scores, return result dict.

Founding rules enforced here:
  Rule 1: No score point without a citing evidence_id → ScoringIntegrityError
  Rule 4: evaluate_mandatory_gates() is the first call — gated companies get no score
  Rule 5: No AI, no LLM, no randomness in any scoring path
"""
from __future__ import annotations

import uuid as _uuid_module
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Company,
    EvidenceItem,
    LeadCandidate,
    LeadScore,
    Signal,
    ScoringConfig,
)
from app.processing.gates import evaluate_mandatory_gates, flag_excluded_sector


# ─── Exceptions ───────────────────────────────────────────────────────────────


class ScoringIntegrityError(Exception):
    """Raised when a scoring component awards points but cites no evidence_ids.

    Enforces founding rule 1: every score point must be traceable to an
    evidence document. Never catch and swallow this — it indicates a bug.
    """


class NoActiveScoringConfigError(Exception):
    """Raised when no active scoring_config row exists.

    The scoring engine requires a versioned, active config so that score
    changes are always attributable to a deliberate config change.
    """


# ─── Constants ────────────────────────────────────────────────────────────────

# NAICS prefixes for A/R-heavy (receivables-intensive) industries.
# Companies in these industries commonly need A/R financing.
_AR_HEAVY_NAICS = ("54", "56", "33", "48", "23", "62")

# Signal types that count as contract award evidence for scoring.
# SUBCONTRACT_AWARD is emitted by the usaspending_subawards connector.
_AWARD_SIGNAL_TYPES = frozenset({"CONTRACT_AWARD", "SUBCONTRACT_AWARD"})

# SBA signal types — bonuses scale with freshness (loan recency).
# Max values confirmed by John Cox Miller, Porter Capital, June 25 2026:
#   PIF max = 8, Active max = 4 (both capped by why_now ceiling of 30).
_SBA_SIGNAL_TYPES = frozenset({"SBA_LOAN_PIF", "SBA_LOAN_ACTIVE"})

# SBIR/STTR grant signal types.
# Bonuses confirmed by John Cox Miller, Porter Capital, July 2026:
#   Phase II/III (strong) → +6, Phase I (medium) → +3
_SBIR_SIGNAL_TYPES = frozenset({"SBIR_GRANT"})

# Phase 1 cap for A/R Financing Fit component.
_AR_FIT_PHASE1_CAP = 10

# Tier thresholds (inclusive lower bound).
_TIER_HOT = 70
_TIER_WARM = 55
_TIER_COLD = 35


# ─── Pure helpers ─────────────────────────────────────────────────────────────


def _sba_why_now_points(signal_type: str, freshness: float) -> int:
    """Tiered why_now bonus for SBA signals based on loan type and recency.

    Freshness 0.0–1.0 is computed from loan approval_date over a 5-year window,
    so 2022 loans score ~0.1 and 2026 loans score ~1.0.

    Max values (freshness=1.0): PIF=8, Active=4 — confirmed by John Cox Miller.
    Using freshness tiers introduces score variation across the loan cohort.
    """
    if signal_type == "SBA_LOAN_PIF":
        if freshness >= 0.8:
            return 8
        if freshness >= 0.5:
            return 6
        if freshness >= 0.1:
            return 4
        return 2
    # SBA_LOAN_ACTIVE
    if freshness >= 0.8:
        return 4
    if freshness >= 0.5:
        return 3
    if freshness >= 0.1:
        return 2
    return 1


def _assign_tier(score: int) -> str:
    """Map a numeric score to a tier label. Deterministic, no side effects."""
    if score >= _TIER_HOT:
        return "hot"
    if score >= _TIER_WARM:
        return "warm"
    if score >= _TIER_COLD:
        return "cold"
    return "archive"


def _is_ar_heavy_naics(naics_code) -> bool:
    if not naics_code:
        return False
    return str(naics_code).startswith(_AR_HEAVY_NAICS)


def _check_integrity(component_name: str, points: int, evidence_ids: list) -> None:
    """Raise ScoringIntegrityError if points > 0 but evidence_ids is empty."""
    if points > 0 and not evidence_ids:
        raise ScoringIntegrityError(
            f"Component '{component_name}' awarded {points} point(s) but cited no "
            "evidence_ids. Every scored point must be traceable to an evidence document "
            "(founding rule 1)."
        )


def _dedup_uuids(ids: list[UUID]) -> list[UUID]:
    """Deduplicate and sort UUID list for deterministic output."""
    return sorted(set(ids), key=str)


def _uuids_to_strings(ids: list[UUID]) -> list[str]:
    """Convert UUID list to sorted deduplicated strings (for JSONB storage)."""
    return sorted({str(i) for i in ids})


# ─── Main entry point ─────────────────────────────────────────────────────────


def score_company(company_id: UUID, db: Session) -> dict:
    """
    Score a company through mandatory gates then six scoring components.

    Returns a dict:
        scored              bool
        company_id          UUID
        gate_result         dict   — full gate result (always present)
        total_score         int | None
        tier                str | None
        component_breakdown dict   — component-level detail including evidence_ids
        evidence_ids        list[UUID]

    Side effects (only for scored=True):
        - Creates or updates one LeadCandidate row (status='active')
        - Inserts one LeadScore row

    No side effects for scored=False (gated companies).
    """
    # ── Step 1: mandatory gates — founding rule 4 ─────────────────────────────
    gate_result = evaluate_mandatory_gates(company_id, db)
    if not gate_result["should_score"]:
        return {
            "scored": False,
            "company_id": company_id,
            "gate_result": gate_result,
            "total_score": None,
            "tier": None,
            "component_breakdown": {},
            "evidence_ids": [],
        }

    # ── Step 2: load and validate active scoring config ───────────────────────
    config_row: ScoringConfig | None = (
        db.execute(
            select(ScoringConfig).where(ScoringConfig.active == True)  # noqa: E712
        )
        .scalars()
        .first()
    )
    if config_row is None:
        raise NoActiveScoringConfigError(
            "No active scoring_config row found. "
            "Create and activate a versioned config before running the scoring engine."
        )
    scoring_config_id: UUID = config_row.id
    config_hash: str = config_row.config_hash

    # ── Step 3: load data for scoring ─────────────────────────────────────────
    company: Company = db.get(Company, company_id)  # type: ignore[assignment]

    evidence_items: list[EvidenceItem] = (
        db.execute(
            select(EvidenceItem).where(EvidenceItem.company_id == company_id)
        )
        .scalars()
        .all()
    )
    signals: list[Signal] = (
        db.execute(
            select(Signal).where(Signal.company_id == company_id)
        )
        .scalars()
        .all()
    )

    all_evidence_ids: list[UUID] = [e.id for e in evidence_items]
    contract_signals = [s for s in signals if s.signal_type in _AWARD_SIGNAL_TYPES]
    award_signals = [s for s in signals if s.award_amount is not None]

    # Accumulate all cited UUIDs (as UUID objects) across all components.
    cited_uuids: set[UUID] = set()
    components: dict = {}

    # ── Component 1: Porter Fit (max 25) ──────────────────────────────────────
    pf_points = 0
    pf_evidence: list[UUID] = []

    if _is_ar_heavy_naics(company.naics_code):
        pf_points += 15
        pf_evidence.extend(all_evidence_ids)

    if award_signals:
        best = max(award_signals, key=lambda s: float(s.award_amount or 0))
        amount = float(best.award_amount)
        if 250_000 <= amount <= 10_000_000:
            pf_points += 5
            pf_evidence.append(best.evidence_id)

    country = company.country
    if not country or country == "US":
        pf_points += 5
        pf_evidence.extend(all_evidence_ids)

    _check_integrity("porter_fit", pf_points, pf_evidence)
    cited_uuids.update(pf_evidence)
    components["porter_fit"] = {
        "points": pf_points,
        "max": 25,
        "evidence_ids": _uuids_to_strings(pf_evidence),
    }

    # ── Component 2: Why-Now / Timing (max 30) ────────────────────────────────
    wn_points = 0
    wn_evidence: list[UUID] = []

    if contract_signals:
        freshest = max(contract_signals, key=lambda s: float(s.freshness_score))
        fs = float(freshest.freshness_score)
        if fs >= 0.8:
            wn_points = 30
        elif fs >= 0.5:
            wn_points = 18
        elif fs >= 0.1:
            wn_points = 8
        if wn_points > 0:
            wn_evidence.append(freshest.evidence_id)

    # SBA why_now bonus: tiered by loan type and recency (freshness_score).
    # Take the best (highest-scoring) SBA signal if multiple exist.
    sba_signals = [s for s in signals if s.signal_type in _SBA_SIGNAL_TYPES]
    if sba_signals:
        best_sba = max(
            sba_signals,
            key=lambda s: _sba_why_now_points(s.signal_type, float(s.freshness_score or 0)),
        )
        sba_bonus = _sba_why_now_points(best_sba.signal_type, float(best_sba.freshness_score or 0))
        if sba_bonus > 0:
            wn_points = min(30, wn_points + sba_bonus)
            wn_evidence.append(best_sba.evidence_id)

    # SBIR/STTR why_now bonus: Phase II/III (strong) → +6, Phase I (medium) → +3.
    # Confirmed by John Cox Miller, Porter Capital, July 2026.
    sbir_signals = [s for s in signals if s.signal_type in _SBIR_SIGNAL_TYPES]
    if sbir_signals:
        best_sbir = max(sbir_signals, key=lambda s: float(s.freshness_score or 0))
        sbir_bonus = 6 if getattr(best_sbir, "signal_strength", "medium") == "strong" else 3
        if sbir_bonus > 0:
            wn_points = min(30, wn_points + sbir_bonus)
            wn_evidence.append(best_sbir.evidence_id)

    _check_integrity("why_now", wn_points, wn_evidence)
    cited_uuids.update(wn_evidence)
    components["why_now"] = {
        "points": wn_points,
        "max": 30,
        "evidence_ids": _uuids_to_strings(wn_evidence),
    }

    # ── Component 3: A/R Financing Fit (max 15, Phase 1 cap = 10) ────────────
    ar_points = 0
    ar_evidence: list[UUID] = []

    if _is_ar_heavy_naics(company.naics_code):
        ar_points += 7
        ar_evidence.extend(all_evidence_ids)

    # SBA loan and SBIR grant signals are also evidence of A/R financing need.
    lending_signals = contract_signals or [
        s for s in signals
        if s.signal_type in (_SBA_SIGNAL_TYPES | _SBIR_SIGNAL_TYPES)
    ]
    if lending_signals:
        ar_points += 3
        ar_evidence.append(lending_signals[0].evidence_id)

    ar_points = min(ar_points, _AR_FIT_PHASE1_CAP)

    _check_integrity("ar_fit", ar_points, ar_evidence)
    cited_uuids.update(ar_evidence)
    components["ar_fit"] = {
        "points": ar_points,
        "max": 15,
        "phase1_cap": _AR_FIT_PHASE1_CAP,
        "ar_fit_confidence": "low",
        "evidence_ids": _uuids_to_strings(ar_evidence),
    }

    # ── Component 4: Contactability (max 10, always 0 in Phase 1) ────────────
    # Paid enrichment and contact verification are not built in Phase 1.
    components["contactability"] = {
        "points": 0,
        "max": 10,
        "evidence_ids": [],
        "note": "Phase 1: contact enrichment not built",
    }

    # ── Component 5: Risk / Clean (max 10) ────────────────────────────────────
    # Phase 1: no dedicated risk-check evidence documents exist.
    # Keep at 0 rather than citing unrelated evidence as a risk clearance.
    components["risk_clean"] = {
        "points": 0,
        "max": 10,
        "evidence_ids": [],
        "note": "Phase 1: dedicated risk evidence not available",
    }

    # ── Component 6: Evidence Quality (max 10) ────────────────────────────────
    eq_points = 0
    eq_evidence: list[UUID] = []

    if len(evidence_items) >= 2:
        eq_points += 5
        eq_evidence.extend(all_evidence_ids[:2])

    source_url_items = [e for e in evidence_items if e.source_url]
    if source_url_items:
        eq_points += 3
        eq_evidence.append(source_url_items[0].id)

    _check_integrity("evidence_quality", eq_points, eq_evidence)
    cited_uuids.update(eq_evidence)
    components["evidence_quality"] = {
        "points": eq_points,
        "max": 10,
        "evidence_ids": _uuids_to_strings(eq_evidence),
    }

    # ── Step 4: totals and tier ───────────────────────────────────────────────
    total_score = sum(c["points"] for c in components.values())
    tier = _assign_tier(total_score)
    all_cited_uuids = _dedup_uuids(list(cited_uuids))

    # ── Step 5: persist lead_candidate ────────────────────────────────────────
    existing_candidate: LeadCandidate | None = (
        db.execute(
            select(LeadCandidate).where(
                LeadCandidate.company_id == company_id,
                LeadCandidate.status == "active",
                LeadCandidate.deleted_at.is_(None),
            )
        )
        .scalars()
        .first()
    )

    if existing_candidate is None:
        candidate = LeadCandidate(
            id=_uuid_module.uuid4(),
            company_id=company_id,
            status="active",
            tier=tier,
            gate_result="passed",
            gate_reason=None,
            current_score=total_score,
            ar_fit_confidence="low",
            sales_status="research",
        )
        db.add(candidate)
    else:
        candidate = existing_candidate
        candidate.tier = tier
        candidate.current_score = total_score
        candidate.gate_result = "passed"
        candidate.ar_fit_confidence = "low"

    # Soft-flag leads whose NAICS falls in a Porter ICP excluded sector.
    # Does not block scoring — lead is stored and scored normally.
    flag_excluded_sector(company, candidate, db)

    # ── Step 6: persist lead_scores ───────────────────────────────────────────
    lead_score = LeadScore(
        id=_uuid_module.uuid4(),
        lead_candidate_id=candidate.id,
        scoring_config_id=scoring_config_id,
        config_hash=config_hash,
        total_score=total_score,
        tier=tier,
        component_breakdown=components,
        evidence_ids=all_cited_uuids,
        gate_result="passed",
        gate_reasons=[],
    )
    db.add(lead_score)

    return {
        "scored": True,
        "company_id": company_id,
        "gate_result": gate_result,
        "total_score": total_score,
        "tier": tier,
        "component_breakdown": components,
        "evidence_ids": all_cited_uuids,
    }
