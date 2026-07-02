from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.schemas import (
    ContactabilitySchema,
    EvidenceItemSchema,
    LeadDetailSchema,
    LeadListItemSchema,
    LeadsListResponse,
    ReviewDecisionSchema,
    ReviewRequest,
    ReviewResponse,
    ScoreDetailSchema,
    SignalSchema,
)
from app.dashboard.review import (
    get_contactability,
    get_lead_detail,
    get_latest_run_context,
    list_leads_filtered,
    submit_lead_review,
)
from app.db.models import EvidenceItem, LeadCandidate, Signal
from app.db.session import get_session

router = APIRouter()

_WARNING = (
    "Research-ready leads only. Not sales-ready. "
    "No verified contacts. No Salesforce push. Human review required."
)

_CONTACT_NOTE = (
    "SAM entity validation only — confirms entity exists in SAM.gov. "
    "Does not confirm a decision-maker email or phone."
)


def _s(value) -> str | None:
    return str(value) if value is not None else None


@router.get("/leads", response_model=LeadsListResponse)
def list_leads(
    view: str = "all",
    source_id: str | None = None,
    tier: str | None = None,
    sales_status: str | None = None,
    has_contactability: bool | None = None,
    has_evidence_url: bool | None = None,
    sort_by: str = "score_desc",
    limit: int = 50,
    offset: int = 0,
    include_excluded: bool = False,
    signal_type: str | None = None,
    db: Session = Depends(get_session),
) -> LeadsListResponse:
    parsed_source_id: UUID | None = None
    if source_id:
        try:
            parsed_source_id = UUID(source_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="source_id must be a valid UUID")

    latest_run_started_at = None
    if view == "latest_run":
        run_ctx = get_latest_run_context(db)
        latest_run_started_at = run_ctx.get("started_at")

    rows = list_leads_filtered(
        db,
        view=view,
        tier=tier,
        source_id=parsed_source_id,
        sales_status=sales_status,
        has_contactability=has_contactability,
        has_evidence_url=has_evidence_url,
        sort_by=sort_by,
        latest_run_started_at=latest_run_started_at,
        include_excluded=include_excluded,
        signal_type=signal_type,
    )

    total = len(rows)
    page_rows = rows[offset: offset + limit]

    # Batch-fetch dominant award signal type for companies on this page.
    # Most-recent signal date wins when both CONTRACT_AWARD and SUBCONTRACT_AWARD exist.
    dominant_signal: dict = {}
    company_ids = [r["lead"].company_id for r in page_rows]
    if company_ids:
        sig_rows = (
            db.query(Signal.company_id, Signal.signal_type, Signal.signal_date)
            .filter(
                Signal.company_id.in_(company_ids),
                Signal.signal_type.in_([
                    "CONTRACT_AWARD",
                    "SUBCONTRACT_AWARD",
                    "SBA_LOAN_PIF",
                    "SBA_LOAN_ACTIVE",
                    "SBA_LOAN_PENDING",
                    "SBIR_GRANT",
                    "FEDERAL_GRANT",
                    "IDV_AWARD",
                ]),
            )
            .order_by(Signal.signal_date.desc())
            .all()
        )
        for sig in sig_rows:
            if sig.company_id not in dominant_signal:
                dominant_signal[sig.company_id] = sig.signal_type

    # Batch-fetch awarding_agency from most recent evidence for companies on this page.
    awarding_agency_by_company: dict = {}
    if company_ids:
        ev_rows = (
            db.query(EvidenceItem.company_id, EvidenceItem.extracted_fields)
            .filter(
                EvidenceItem.company_id.in_(company_ids),
                EvidenceItem.extracted_fields.isnot(None),
            )
            .order_by(EvidenceItem.captured_at.desc())
            .all()
        )
        for ev in ev_rows:
            if ev.company_id not in awarding_agency_by_company:
                agency = (ev.extracted_fields or {}).get("awarding_agency")
                if agency:
                    awarding_agency_by_company[ev.company_id] = agency

    items = [
        LeadListItemSchema(
            lead_id=str(r["lead"].id),
            company_id=str(r["lead"].company_id),
            company_name=(
                r["company"].canonical_name if r["company"] else str(r["lead"].company_id)
            ),
            tier=r["lead"].tier,
            score=r["lead"].current_score,
            sales_status=r["lead"].sales_status or "",
            primary_source=r["primary_source"],
            signal_type=dominant_signal.get(r["lead"].company_id),
            latest_signal_date=_s(r["latest_signal_date"]),
            max_award_amount=_s(r["max_award_amount"]),
            is_new_in_run=r["is_new_in_run"],
            created_at=str(r["lead"].created_at),
            updated_at=str(r["lead"].updated_at),
            sector_excluded=bool(r["lead"].sector_excluded),
            sector_excluded_reason=r["lead"].sector_excluded_reason,
            company_naics=r["company"].naics_code if r["company"] else None,
            company_naics_description=r["company"].naics_description if r["company"] else None,
            company_state=r["company"].state if r["company"] else None,
            awarding_agency=awarding_agency_by_company.get(r["lead"].company_id),
        )
        for r in page_rows
    ]

    return LeadsListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        warning=_WARNING,
    )


@router.post("/leads/{lead_id}/review", response_model=ReviewResponse)
def create_lead_review(
    lead_id: UUID,
    body: ReviewRequest,
    db: Session = Depends(get_session),
) -> ReviewResponse:
    lead = db.get(LeadCandidate, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    try:
        review = submit_lead_review(
            lead_candidate_id=lead_id,
            decision=body.decision,
            note=body.note,
            reviewer=body.reviewer,
            db=db,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return ReviewResponse(
        lead_id=str(lead_id),
        decision=body.decision,
        reviewed_at=str(review.decided_at),
        note=body.note,
    )


@router.get("/leads/{lead_id}", response_model=LeadDetailSchema)
def get_lead(lead_id: UUID, db: Session = Depends(get_session)) -> LeadDetailSchema:
    detail = get_lead_detail(lead_id, db)
    if detail is None:
        raise HTTPException(status_code=404, detail="Lead not found")

    lead = detail["lead"]
    company = detail["company"]
    latest_score = detail["latest_score"]
    contact = get_contactability(lead.company_id, db)

    score_schema = None
    if latest_score:
        score_schema = ScoreDetailSchema(
            total_score=latest_score.total_score,
            tier=latest_score.tier,
            gate_result=latest_score.gate_result,
            component_breakdown=latest_score.component_breakdown or {},
            config_hash=latest_score.config_hash,
        )

    evidence_list = [
        EvidenceItemSchema(
            id=str(ev.id),
            claim_supported=ev.claim_supported,
            confidence_score=float(ev.confidence_score),
            source_url=ev.source_url,
            captured_at=str(ev.captured_at),
            extracted_fields=ev.extracted_fields,
        )
        for ev in detail["evidence"]
    ]

    signal_list = [
        SignalSchema(
            id=str(sig.id),
            signal_type=sig.signal_type,
            signal_date=str(sig.signal_date),
            signal_strength=sig.signal_strength,
            award_amount=_s(sig.award_amount),
        )
        for sig in detail["signals"]
    ]

    history_list = [
        ReviewDecisionSchema(
            id=str(dec.id),
            action=dec.action,
            reviewer_id=dec.reviewer_id,
            decided_at=str(dec.decided_at),
            note=dec.note,
        )
        for dec in detail["review_history"]
    ]

    contact_schema = None
    if contact:
        contact_schema = ContactabilitySchema(
            contactability_status=contact.contactability_status,
            contactability_score=contact.contactability_score,
            sam_match_status=contact.sam_match_status,
            sam_uei=contact.sam_uei,
            sam_registration_status=contact.sam_registration_status,
            official_website=contact.official_website,
            phone=contact.phone,
            generic_email=contact.generic_email,
            last_checked_at=str(contact.last_checked_at),
            sam_note=_CONTACT_NOTE,
        )

    return LeadDetailSchema(
        lead_id=str(lead.id),
        company_id=str(lead.company_id),
        company_name=company.canonical_name if company else str(lead.company_id),
        company_state=company.state if company else None,
        company_naics=company.naics_code if company else None,
        company_naics_description=company.naics_description if company else None,
        company_domain=company.website_domain if company else None,
        company_industry=company.industry if company else None,
        company_business_type=company.business_type if company else None,
        tier=lead.tier,
        score=lead.current_score,
        sales_status=lead.sales_status or "",
        gate_result=lead.gate_result,
        gate_reason=lead.gate_reason,
        ar_fit_confidence=lead.ar_fit_confidence,
        why_now_summary=lead.why_now_summary,
        latest_score=score_schema,
        evidence=evidence_list,
        signals=signal_list,
        review_history=history_list,
        contactability=contact_schema,
        warning=_WARNING,
        contactability_note=_CONTACT_NOTE if contact else "",
    )
