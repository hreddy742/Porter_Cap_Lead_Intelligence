"""
Phase 1 end-to-end smoke demo.

PURPOSE
-------
Proves the full Phase 1 processing chain works with deterministic sample data:

  raw_source_events
    → evidence_items       (extract_evidence)
    → companies            (resolve_company_for_evidence)
    → signals              (detect_signals_for_evidence)
    → lead_candidates      (score_company — mandatory gates then scoring)
    → review_decisions     (action="approve")
    → CSV export           (exports/demo_approved_leads.csv)

DEMO DATA INSERTED INTO LOCAL DB
---------------------------------
  source_registry     name = "usaspending_demo"
  companies           canonical_name = "Demo Contracting LLC"  (UEI: DEMO0000000001)
  raw_source_events   source_record_id = "DEMO-AWARD-2026-001"
  review_decisions    reviewer_id = "demo@portercapital.local", action = "approve"

IDEMPOTENCY
-----------
Repeated runs are safe.  Existing demo rows are detected via hard identifiers
and reused without creating duplicates.  On re-run, the scoring gate fires
"duplicate_active" (expected), the existing lead candidate is found, and the
CSV is re-exported successfully.

CONSTRAINTS
-----------
  - Does NOT call the real USASpending API.
  - Does NOT call Salesforce.
  - Does NOT use AI / LLM.
  - Does NOT add cron or monitoring.
  - All sample data is deterministic.

WARNING: Requires a running local PostgreSQL instance.
         Do NOT run against production databases.

USAGE
-----
  python scripts/smoke_demo.py
"""
from __future__ import annotations

import hashlib
import json
import sys
import traceback
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

# ── Module-level constants (importable by tests without running main()) ────────

DEMO_SOURCE_NAME = "usaspending_demo"
DEMO_SOURCE_RECORD_ID = "DEMO-AWARD-2026-001"
DEMO_REVIEWER_ID = "demo@portercapital.local"
DEMO_CSV_PATH = "exports/demo_approved_leads.csv"
DEMO_UEI = "DEMO0000000001"

DEMO_PAYLOAD: dict = {
    "Award ID": DEMO_SOURCE_RECORD_ID,
    "Recipient Name": "Demo Contracting LLC",
    "Recipient UEI": DEMO_UEI,
    "Start Date": "2026-05-01",
    "Award Amount": 2500000.00,
    "NAICS Code": "541511",
    "NAICS Description": "Custom Computer Programming Services",
    "Place of Performance State Code": "VA",
    "Award Type": "DEFINITIVE CONTRACT",
    "Awarding Agency": "DEPARTMENT OF DEFENSE",
}


# ── Pure helpers ───────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _sha256_dict(d: dict) -> str:
    return hashlib.sha256(
        json.dumps(d, sort_keys=True, default=str).encode()
    ).hexdigest()


# ── Idempotent setup helpers ───────────────────────────────────────────────────

def _ensure_source_registry(db: Session):
    """Return existing or newly created demo source_registry row.

    The demo source is intentionally NOT enabled so that it does not appear in
    real pipeline runs (orchestrator only processes enabled sources).
    status='planned' + enabled=False is the correct state for a demo-only source.
    """
    from app.db.models import SourceRegistry

    existing = db.execute(
        select(SourceRegistry).where(SourceRegistry.name == DEMO_SOURCE_NAME)
    ).scalar_one_or_none()
    if existing:
        # Ensure existing demo row is not accidentally enabled from a previous
        # version of this script that set enabled=True.
        if existing.enabled:
            existing.enabled = False
            existing.status = "planned"
            db.flush()
        return existing

    source = SourceRegistry(
        name=DEMO_SOURCE_NAME,
        category="federal_contracts",
        access_method="api",
        status="planned",
        enabled=False,
        cost_type="free",
        signal_types=["CONTRACT_AWARD"],
        base_url="https://api.usaspending.gov",
        quality_score=Decimal("0.90"),
    )
    db.add(source)
    db.flush()
    return source


def _ensure_scoring_config(db: Session):
    """Return existing active scoring_config or create one if none exists."""
    from app.db.models import ScoringConfig

    existing = db.execute(
        select(ScoringConfig).where(ScoringConfig.active == True)  # noqa: E712
    ).scalar_one_or_none()
    if existing:
        return existing

    config = {
        "version": "v1.0",
        "tier_thresholds": {"hot": 75, "warm": 55, "cold": 35},
        "components": {
            "porter_fit": {"max": 25},
            "why_now": {"max": 30},
            "ar_fit": {"max": 15, "phase1_cap": 10},
            "contactability": {"max": 10, "phase1_value": 0},
            "risk_clean": {"max": 10, "phase1_value": 0},
            "evidence_quality": {"max": 10},
        },
        "note": "Phase 1 scoring config — seeded by smoke_demo.py",
    }
    config_hash = _sha256_dict(config)

    scoring_config = ScoringConfig(
        version_label="v1.0",
        config=config,
        config_hash=config_hash,
        active=True,
        notes="Phase 1 scoring config — auto-seeded by smoke_demo.py.",
        created_by="smoke_demo",
    )
    db.add(scoring_config)
    db.flush()
    return scoring_config


def _create_pipeline_and_source_run(source, db: Session):
    """Create a completed pipeline_run + source_run for the demo raw event's FK."""
    from app.db.models import PipelineRun, SourceRun

    now = _utcnow()
    pipeline_run = PipelineRun(
        status="completed",
        trigger="smoke_demo",
        started_at=now,
        ended_at=now,
        total_records=1,
    )
    db.add(pipeline_run)
    db.flush()

    source_run = SourceRun(
        pipeline_run_id=pipeline_run.id,
        source_id=source.id,
        status="completed",
        started_at=now,
        finished_at=now,
        records_fetched=1,
        records_valid=1,
    )
    db.add(source_run)
    db.flush()
    return pipeline_run, source_run


def _ensure_raw_event(source, source_run, db: Session) -> tuple:
    """Return (raw_event, is_new).  Existing row detected by source_record_id."""
    from app.db.models import RawSourceEvent

    existing = db.execute(
        select(RawSourceEvent).where(
            RawSourceEvent.source_id == source.id,
            RawSourceEvent.source_record_id == DEMO_SOURCE_RECORD_ID,
        )
    ).scalar_one_or_none()
    if existing:
        return existing, False

    content_hash = _sha256_dict(DEMO_PAYLOAD)
    raw_event = RawSourceEvent(
        source_id=source.id,
        source_run_id=source_run.id,
        source_record_id=DEMO_SOURCE_RECORD_ID,
        company_name_raw=DEMO_PAYLOAD["Recipient Name"],
        payload=DEMO_PAYLOAD,
        content_hash=content_hash,
        source_url=(
            f"https://www.usaspending.gov/award/{DEMO_SOURCE_RECORD_ID}/"
        ),
    )
    db.add(raw_event)
    db.flush()
    return raw_event, True


def _ensure_evidence(raw_event, db: Session):
    """Return existing evidence for raw_event or extract fresh evidence."""
    from app.db.models import EvidenceItem
    from app.processing.evidence import extract_evidence

    existing = db.execute(
        select(EvidenceItem).where(EvidenceItem.raw_event_id == raw_event.id)
    ).scalars().first()
    if existing:
        return existing

    items = extract_evidence(raw_event.id, db)
    if not items:
        raise RuntimeError(
            "extract_evidence returned no items.  "
            "Check that DEMO_PAYLOAD has 'Recipient Name' and 'Start Date' set correctly."
        )
    return items[0]


def _ensure_signal(evidence, db: Session):
    """Return existing signal for evidence or detect a fresh one."""
    from app.db.models import Signal
    from app.processing.signals import detect_signals_for_evidence

    existing = db.execute(
        select(Signal).where(Signal.evidence_id == evidence.id)
    ).scalars().first()
    if existing:
        return existing

    signals = detect_signals_for_evidence(evidence.id, db)
    if not signals:
        raise RuntimeError(
            "detect_signals_for_evidence returned no signals.  "
            "Ensure evidence.company_id is set (resolution must run first)."
        )
    return signals[0]


def _ensure_review_decision(lead_candidate, db: Session):
    """Return existing demo approve decision or insert a new one."""
    from app.db.models import ReviewDecision

    existing = db.execute(
        select(ReviewDecision).where(
            ReviewDecision.lead_candidate_id == lead_candidate.id,
            ReviewDecision.reviewer_id == DEMO_REVIEWER_ID,
            ReviewDecision.action == "approve",
        )
    ).scalar_one_or_none()
    if existing:
        return existing

    decision = ReviewDecision(
        lead_candidate_id=lead_candidate.id,
        reviewer_id=DEMO_REVIEWER_ID,
        action="approve",
        note="Smoke demo approval — auto-generated by smoke_demo.py",
    )
    db.add(decision)
    db.flush()
    return decision


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> int:
    """
    Run the Phase 1 end-to-end smoke demo.

    All processing runs in a single transaction that commits once at the end.
    CSV export reads the committed data.

    Returns 0 on full success, 1 on any failure.
    """
    from app.db.session import SessionLocal
    from app.db.models import LeadCandidate
    from app.processing.resolution import resolve_company_for_evidence
    from app.processing.scoring import score_company
    from app.export.csv_export import export_approved_leads_csv

    _sep = "=" * 62
    print(f"\n{_sep}")
    print("  Porter Capital — Phase 1 Smoke Demo")
    print(_sep)

    db: Session = SessionLocal()
    try:
        # ── [1] Setup ──────────────────────────────────────────────────────────
        print("\n[1/7] Setting up demo infrastructure...")
        source = _ensure_source_registry(db)
        print(f"      source_registry id    = {source.id}")

        scoring_config = _ensure_scoring_config(db)
        print(
            f"      scoring_config id     = {scoring_config.id}"
            f"  (version={scoring_config.version_label})"
        )

        _pipeline_run, source_run = _create_pipeline_and_source_run(source, db)

        # ── [2] Raw event ──────────────────────────────────────────────────────
        print("\n[2/7] Inserting demo raw_source_event...")
        raw_event, is_new = _ensure_raw_event(source, source_run, db)
        status_tag = "created" if is_new else "existing (idempotent re-run)"
        print(f"      raw_event id          = {raw_event.id}  [{status_tag}]")
        print(f"      source_record_id      = {raw_event.source_record_id}")

        # ── [3] Evidence ───────────────────────────────────────────────────────
        print("\n[3/7] extract_evidence...")
        evidence = _ensure_evidence(raw_event, db)
        print(f"      evidence_item id      = {evidence.id}")
        print(f"      claim_supported       = {evidence.claim_supported}")
        print(f"      freshness_score       = {float(evidence.freshness_score):.4f}")

        # ── [4] Company resolution ─────────────────────────────────────────────
        print("\n[4/7] resolve_company_for_evidence...")
        company = resolve_company_for_evidence(evidence.id, db)
        if company is None:
            print("FAIL: resolve_company_for_evidence returned None")
            db.rollback()
            return 1
        print(f"      company_id            = {company.id}")
        print(f"      canonical_name        = {company.canonical_name}")

        # Propagate NAICS from evidence to company if not yet set.
        # In production this would be done by a dedicated enrichment step;
        # in the smoke demo we do it explicitly so scoring has NAICS data.
        naics_from_evidence = (evidence.extracted_fields or {}).get("naics_code")
        if naics_from_evidence and not company.naics_code:
            company.naics_code = str(naics_from_evidence)
            db.flush()
        print(f"      naics_code            = {company.naics_code or '(not set)'}")

        # ── [5] Signal detection ───────────────────────────────────────────────
        print("\n[5/7] detect_signals_for_evidence...")
        signal = _ensure_signal(evidence, db)
        print(f"      signal_id             = {signal.id}")
        print(f"      signal_type           = {signal.signal_type}")
        print(f"      signal_strength       = {signal.signal_strength}")
        print(f"      award_amount          = {signal.award_amount}")

        # ── [6] Scoring (includes mandatory gates) ─────────────────────────────
        print("\n[6/7] evaluate_mandatory_gates + score_company...")
        score_result = score_company(company.id, db)
        gate_result = score_result["gate_result"]
        print(f"      gate passed           = {gate_result['passed']}")

        if not gate_result["passed"]:
            gate_reason = gate_result.get("gate_reason")
            print(f"      gate_reason           = {gate_reason}")
            if gate_reason == "duplicate_active":
                print(
                    "      (expected on re-run: "
                    "existing lead_candidate reused from first run)"
                )

        # Retrieve the active lead_candidate whether scoring created it
        # (first run) or it already existed (re-run via duplicate_active gate).
        lead_candidate = db.execute(
            select(LeadCandidate).where(
                LeadCandidate.company_id == company.id,
                LeadCandidate.status == "active",
                LeadCandidate.deleted_at.is_(None),
            )
        ).scalars().first()

        if lead_candidate is None:
            print(
                f"FAIL: no active lead_candidate found after scoring.  "
                f"Gate result: {gate_result}"
            )
            db.rollback()
            return 1

        print(f"      lead_candidate_id     = {lead_candidate.id}")
        print(f"      total_score / tier    = {lead_candidate.current_score} / {lead_candidate.tier}")

        # ── [7] Review decision ────────────────────────────────────────────────
        print("\n[7/7] Creating review decision (action=approve)...")
        decision = _ensure_review_decision(lead_candidate, db)
        print(f"      review_decision id    = {decision.id}")
        print(f"      reviewer_id           = {decision.reviewer_id}")
        print(f"      action                = {decision.action}")

        # ── Commit ─────────────────────────────────────────────────────────────
        db.commit()
        print("\n      [OK] Transaction committed.")

        # ── [8] CSV export (reads committed data) ─────────────────────────────
        print(f"\n[8/8] Exporting approved leads -> {DEMO_CSV_PATH} ...")
        export_result = export_approved_leads_csv(DEMO_CSV_PATH, db)
        print(f"      exported_count        = {export_result['exported_count']}")
        print(f"      skipped_count         = {export_result['skipped_count']}")
        print(f"      output_path           = {export_result['output_path']}")

        if export_result["exported_count"] < 1:
            print("\nFAIL: CSV export produced 0 rows — demo lead was not exported.")
            return 1

        # ── Summary ────────────────────────────────────────────────────────────
        print(f"\n{_sep}")
        print("  SMOKE DEMO PASSED")
        print(_sep)
        print(f"  raw_event_id        : {raw_event.id}")
        print(f"  evidence_id         : {evidence.id}")
        print(f"  company_id          : {company.id}")
        print(f"  signal_id           : {signal.id}")
        print(f"  lead_candidate_id   : {lead_candidate.id}")
        print(
            f"  total_score / tier  : "
            f"{lead_candidate.current_score} / {lead_candidate.tier}"
        )
        print(f"  review action       : {decision.action}")
        print(f"  CSV path            : {DEMO_CSV_PATH}")
        print(_sep)
        return 0

    except Exception as exc:
        db.rollback()
        print(f"\nSMOKE DEMO FAILED: {exc}")
        traceback.print_exc()
        return 1

    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
