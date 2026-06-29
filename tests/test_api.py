"""
API endpoint tests — Stage 2A.

All DB tests require testcontainers (marked with @pytest.mark.db).
TestNoWriteEndpoints does not touch the DB (write routes simply do not exist).
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from app.db.session import get_session


@pytest.fixture
def client(db_session):
    """TestClient with the testcontainer DB session injected."""
    def override():
        yield db_session

    app.dependency_overrides[get_session] = override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ── Health ────────────────────────────────────────────────────────────────────

@pytest.mark.db
class TestHealthEndpoint:
    def test_returns_200(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200

    def test_status_ok(self, client):
        data = client.get("/api/health").json()
        assert data["status"] == "ok"

    def test_db_connected(self, client):
        data = client.get("/api/health").json()
        assert data["db"] == "connected"


# ── Dashboard summary ─────────────────────────────────────────────────────────

@pytest.mark.db
class TestDashboardSummary:
    def test_returns_200(self, client):
        r = client.get("/api/dashboard/summary")
        assert r.status_code == 200

    def test_has_required_keys(self, client):
        data = client.get("/api/dashboard/summary").json()
        assert "run_context" in data
        assert "tier_counts" in data
        assert "warning" in data

    def test_warning_is_not_sales_ready(self, client):
        data = client.get("/api/dashboard/summary").json()
        assert "not sales-ready" in data["warning"].lower()

    def test_tier_counts_zero_on_empty_db(self, client):
        data = client.get("/api/dashboard/summary").json()
        tc = data["tier_counts"]
        assert tc["hot"] == 0
        assert tc["warm"] == 0
        assert tc["cold"] == 0
        assert tc["total"] == 0
        assert tc["pending_review"] == 0

    def test_run_context_no_runs_on_empty_db(self, client):
        data = client.get("/api/dashboard/summary").json()
        assert data["run_context"]["has_runs"] is False
        assert data["run_context"]["pipeline_run_id"] is None


# ── Leads list ────────────────────────────────────────────────────────────────

@pytest.mark.db
class TestLeadsList:
    def test_returns_200(self, client):
        r = client.get("/api/leads")
        assert r.status_code == 200

    def test_has_pagination_fields(self, client):
        data = client.get("/api/leads").json()
        assert "items" in data
        assert "total" in data
        assert "limit" in data
        assert "offset" in data

    def test_has_warning(self, client):
        data = client.get("/api/leads").json()
        assert "warning" in data
        assert "not sales-ready" in data["warning"].lower()

    def test_empty_on_clean_db(self, client):
        data = client.get("/api/leads").json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_limit_reflected_in_response(self, client):
        data = client.get("/api/leads?limit=10").json()
        assert data["limit"] == 10

    def test_offset_reflected_in_response(self, client):
        data = client.get("/api/leads?offset=5").json()
        assert data["offset"] == 5

    def test_invalid_source_id_returns_422(self, client):
        r = client.get("/api/leads?source_id=not-a-uuid")
        assert r.status_code == 422

    def test_view_param_accepted(self, client):
        for view in ("all", "today", "recently_updated", "latest_run"):
            r = client.get(f"/api/leads?view={view}")
            assert r.status_code == 200, f"view={view} failed"

    def test_tier_param_accepted(self, client):
        for tier in ("hot", "warm", "cold", "archive"):
            r = client.get(f"/api/leads?tier={tier}")
            assert r.status_code == 200

    @pytest.mark.db
    def test_archive_tier_filter_returns_archive_leads(self, client, db_session):
        """Archive leads appear in /api/leads?tier=archive and not in other tiers."""
        import uuid as uuid_mod
        from app.db.models import Company, LeadCandidate

        company = Company(
            id=uuid_mod.uuid4(),
            canonical_name="Archive Filter Test Co",
            normalized_name="archive filter test co",
            external_id="archtest00123456",
            country="US",
        )
        db_session.add(company)
        db_session.flush()

        lead = LeadCandidate(
            id=uuid_mod.uuid4(),
            company_id=company.id,
            status="active",
            tier="archive",
            current_score=10,
            sales_status="research",
        )
        db_session.add(lead)
        db_session.flush()

        r = client.get("/api/leads?tier=archive")
        assert r.status_code == 200
        data = r.json()
        names = [i["company_name"] for i in data["items"]]
        assert "Archive Filter Test Co" in names, "archive lead missing from tier=archive response"

        r_hot = client.get("/api/leads?tier=hot")
        hot_names = [i["company_name"] for i in r_hot.json()["items"]]
        assert "Archive Filter Test Co" not in hot_names

    def test_sort_param_accepted(self, client):
        for sort in ("score_desc", "newest_first", "latest_updated"):
            r = client.get(f"/api/leads?sort_by={sort}")
            assert r.status_code == 200


# ── Lead detail ───────────────────────────────────────────────────────────────

@pytest.mark.db
class TestLeadDetail:
    def test_nonexistent_lead_returns_404(self, client):
        r = client.get(f"/api/leads/{uuid.uuid4()}")
        assert r.status_code == 404

    def test_404_response_has_detail(self, client):
        data = client.get(f"/api/leads/{uuid.uuid4()}").json()
        assert "detail" in data

    def test_invalid_uuid_path_returns_422(self, client):
        r = client.get("/api/leads/not-a-uuid")
        assert r.status_code == 422


# ── No write endpoints ────────────────────────────────────────────────────────

class TestNoWriteEndpoints:
    """Verify no write endpoints exist. These do not touch the DB."""

    @pytest.fixture
    def plain_client(self):
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c

    def test_no_post_leads(self, plain_client):
        r = plain_client.post("/api/leads", json={})
        assert r.status_code == 405

    def test_no_put_lead(self, plain_client):
        r = plain_client.put(f"/api/leads/{uuid.uuid4()}", json={})
        assert r.status_code == 405

    def test_no_delete_lead(self, plain_client):
        r = plain_client.delete(f"/api/leads/{uuid.uuid4()}")
        assert r.status_code == 405

    def test_no_post_review_decisions(self, plain_client):
        r = plain_client.post("/api/review-decisions", json={})
        assert r.status_code == 404  # route does not exist at all


# ── signal_type field ─────────────────────────────────────────────────────────

@pytest.mark.db
class TestLeadReview:
    def _make_lead(self, db_session):
        import uuid as uuid_mod
        from app.db.models import Company, LeadCandidate

        company = Company(
            id=uuid_mod.uuid4(),
            canonical_name=f"Review Test Co {uuid_mod.uuid4().hex[:6]}",
            normalized_name="review test co",
            external_id=f"revtest{uuid_mod.uuid4().hex[:8]}",
            country="US",
        )
        db_session.add(company)
        db_session.flush()

        lead = LeadCandidate(
            id=uuid_mod.uuid4(),
            company_id=company.id,
            status="active",
            tier="warm",
            current_score=55,
            sales_status="research",
        )
        db_session.add(lead)
        db_session.flush()
        return company, lead

    def test_approve_returns_200(self, client, db_session):
        _, lead = self._make_lead(db_session)
        r = client.post(f"/api/leads/{lead.id}/review", json={"decision": "approved"})
        assert r.status_code == 200

    def test_reject_returns_200(self, client, db_session):
        _, lead = self._make_lead(db_session)
        r = client.post(f"/api/leads/{lead.id}/review", json={"decision": "rejected"})
        assert r.status_code == 200

    def test_contacted_returns_200(self, client, db_session):
        _, lead = self._make_lead(db_session)
        r = client.post(f"/api/leads/{lead.id}/review", json={"decision": "contacted"})
        assert r.status_code == 200

    def test_invalid_decision_returns_422(self, client, db_session):
        _, lead = self._make_lead(db_session)
        r = client.post(f"/api/leads/{lead.id}/review", json={"decision": "nuke_it"})
        assert r.status_code == 422

    def test_nonexistent_lead_returns_404(self, client):
        r = client.post(f"/api/leads/{uuid.uuid4()}/review", json={"decision": "approved"})
        assert r.status_code == 404

    def test_response_contains_lead_id_and_decision(self, client, db_session):
        _, lead = self._make_lead(db_session)
        r = client.post(f"/api/leads/{lead.id}/review", json={"decision": "qualified"})
        data = r.json()
        assert data["lead_id"] == str(lead.id)
        assert data["decision"] == "qualified"
        assert "reviewed_at" in data

    def test_review_creates_db_record(self, client, db_session):
        from sqlalchemy import select
        from app.db.models import ReviewDecision

        _, lead = self._make_lead(db_session)
        lead_id = lead.id
        client.post(f"/api/leads/{lead_id}/review", json={"decision": "contacted"})

        db_session.expire_all()
        decisions = db_session.execute(
            select(ReviewDecision).where(ReviewDecision.lead_candidate_id == lead_id)
        ).scalars().all()
        assert len(decisions) == 1
        assert decisions[0].action == "contacted"

    def test_second_review_appends_not_updates(self, client, db_session):
        from sqlalchemy import select
        from app.db.models import ReviewDecision

        _, lead = self._make_lead(db_session)
        lead_id = lead.id
        client.post(f"/api/leads/{lead_id}/review", json={"decision": "contacted"})
        client.post(f"/api/leads/{lead_id}/review", json={"decision": "approved"})

        db_session.expire_all()
        decisions = db_session.execute(
            select(ReviewDecision).where(ReviewDecision.lead_candidate_id == lead_id)
        ).scalars().all()
        assert len(decisions) == 2, "append-only: must create new row, not update"

    def test_sales_status_updated_after_review(self, client, db_session):
        from app.db.models import LeadCandidate as LC

        _, lead = self._make_lead(db_session)
        lead_id = lead.id
        client.post(f"/api/leads/{lead_id}/review", json={"decision": "approved"})

        db_session.expire_all()
        updated = db_session.get(LC, lead_id)
        assert updated.sales_status == "approved"

    def test_review_history_visible_in_get_lead(self, client, db_session):
        _, lead = self._make_lead(db_session)
        client.post(
            f"/api/leads/{lead.id}/review",
            json={"decision": "approved", "note": "promising"},
        )
        db_session.expire_all()
        r = client.get(f"/api/leads/{lead.id}")
        assert r.status_code == 200
        data = r.json()
        assert len(data["review_history"]) == 1
        assert data["review_history"][0]["action"] == "approved"
        assert data["review_history"][0]["note"] == "promising"

    def test_note_is_optional(self, client, db_session):
        _, lead = self._make_lead(db_session)
        r = client.post(f"/api/leads/{lead.id}/review", json={"decision": "passed"})
        assert r.status_code == 200
        assert r.json()["note"] is None

    def test_custom_reviewer_stored(self, client, db_session):
        from sqlalchemy import select
        from app.db.models import ReviewDecision

        _, lead = self._make_lead(db_session)
        lead_id = lead.id
        client.post(
            f"/api/leads/{lead_id}/review",
            json={"decision": "approved", "reviewer": "Jane Smith"},
        )
        db_session.expire_all()
        dec = db_session.execute(
            select(ReviewDecision).where(ReviewDecision.lead_candidate_id == lead_id)
        ).scalars().first()
        assert dec.reviewer_id == "Jane Smith"


class TestSignalTypeField:
    """signal_type appears in leads list items; None when no award signal exists."""

    def test_schema_has_signal_type(self):
        from app.api.schemas import LeadListItemSchema
        assert "signal_type" in LeadListItemSchema.model_fields

    def test_signal_type_defaults_to_none(self):
        from app.api.schemas import LeadListItemSchema
        item = LeadListItemSchema(
            lead_id="00000000-0000-0000-0000-000000000001",
            company_id="00000000-0000-0000-0000-000000000002",
            company_name="Test Co",
            tier="hot",
            score=80,
            sales_status="research",
            primary_source="usaspending",
            latest_signal_date=None,
            max_award_amount=None,
            is_new_in_run=False,
            created_at="2026-01-01T00:00:00",
            updated_at="2026-01-01T00:00:00",
        )
        assert item.signal_type is None

    @pytest.mark.db
    def test_signal_type_in_api_response_item(self, client, db_session):
        """API returns signal_type=None for a lead that has no award signals."""
        import uuid as uuid_mod
        from app.db.models import Company, LeadCandidate

        company = Company(
            id=uuid_mod.uuid4(),
            canonical_name="Signal Type Test Co",
            normalized_name="signal type test co",
            external_id="sigtest0001234567",
            country="US",
        )
        db_session.add(company)
        db_session.flush()

        lead = LeadCandidate(
            id=uuid_mod.uuid4(),
            company_id=company.id,
            status="active",
            sales_status="research",
        )
        db_session.add(lead)
        db_session.flush()

        r = client.get("/api/leads")
        assert r.status_code == 200
        data = r.json()
        items = data["items"]
        assert len(items) >= 1
        match = next(
            (i for i in items if i["company_name"] == "Signal Type Test Co"), None
        )
        assert match is not None, "inserted lead not found in API response"
        assert "signal_type" in match
        assert match["signal_type"] is None


# ── sector_excluded filter ────────────────────────────────────────────────────


class TestSectorExcludedField:
    """sector_excluded fields present in response; default view hides excluded leads."""

    def test_schema_has_sector_excluded(self):
        from app.api.schemas import LeadListItemSchema
        assert "sector_excluded" in LeadListItemSchema.model_fields

    def test_schema_has_sector_excluded_reason(self):
        from app.api.schemas import LeadListItemSchema
        assert "sector_excluded_reason" in LeadListItemSchema.model_fields

    def test_sector_excluded_defaults_to_false(self):
        from app.api.schemas import LeadListItemSchema
        item = LeadListItemSchema(
            lead_id="00000000-0000-0000-0000-000000000001",
            company_id="00000000-0000-0000-0000-000000000002",
            company_name="Test Co",
            tier="hot",
            score=80,
            sales_status="research",
            primary_source="usaspending",
            latest_signal_date=None,
            max_award_amount=None,
            is_new_in_run=False,
            created_at="2026-01-01T00:00:00",
            updated_at="2026-01-01T00:00:00",
        )
        assert item.sector_excluded is False
        assert item.sector_excluded_reason is None

    @pytest.mark.db
    def test_default_view_excludes_sector_excluded_leads(self, client, db_session):
        """GET /api/leads (default) does not return leads with sector_excluded=True."""
        import uuid as uuid_mod
        from app.db.models import Company, LeadCandidate

        company = Company(
            id=uuid_mod.uuid4(),
            canonical_name="Healthcare Sector Co",
            normalized_name="healthcare sector co",
            external_id="hcsector0012345",
            country="US",
            naics_code="621100",
        )
        db_session.add(company)
        db_session.flush()

        lead = LeadCandidate(
            id=uuid_mod.uuid4(),
            company_id=company.id,
            status="active",
            sales_status="research",
            sector_excluded=True,
            sector_excluded_reason="NAICS 621100 is in excluded sector 62",
        )
        db_session.add(lead)
        db_session.flush()

        r = client.get("/api/leads")
        assert r.status_code == 200
        names = [i["company_name"] for i in r.json()["items"]]
        assert "Healthcare Sector Co" not in names, "excluded lead should be hidden by default"

    @pytest.mark.db
    def test_include_excluded_true_returns_all_leads(self, client, db_session):
        """GET /api/leads?include_excluded=true returns leads with sector_excluded=True."""
        import uuid as uuid_mod
        from app.db.models import Company, LeadCandidate

        company = Company(
            id=uuid_mod.uuid4(),
            canonical_name="Construction Sector Co",
            normalized_name="construction sector co",
            external_id="constsect001234",
            country="US",
            naics_code="236110",
        )
        db_session.add(company)
        db_session.flush()

        lead = LeadCandidate(
            id=uuid_mod.uuid4(),
            company_id=company.id,
            status="active",
            sales_status="research",
            sector_excluded=True,
            sector_excluded_reason="NAICS 236110 is in excluded sector 23",
        )
        db_session.add(lead)
        db_session.flush()

        r = client.get("/api/leads?include_excluded=true")
        assert r.status_code == 200
        names = [i["company_name"] for i in r.json()["items"]]
        assert "Construction Sector Co" in names, "excluded lead should appear when toggle is on"

    @pytest.mark.db
    def test_sector_excluded_field_present_in_response(self, client, db_session):
        """Each lead item in the API response includes sector_excluded and sector_excluded_reason."""
        import uuid as uuid_mod
        from app.db.models import Company, LeadCandidate

        company = Company(
            id=uuid_mod.uuid4(),
            canonical_name="Field Check Co",
            normalized_name="field check co",
            external_id="fieldcheck01234",
            country="US",
        )
        db_session.add(company)
        db_session.flush()

        lead = LeadCandidate(
            id=uuid_mod.uuid4(),
            company_id=company.id,
            status="active",
            sales_status="research",
        )
        db_session.add(lead)
        db_session.flush()

        r = client.get("/api/leads")
        assert r.status_code == 200
        items = r.json()["items"]
        match = next((i for i in items if i["company_name"] == "Field Check Co"), None)
        assert match is not None
        assert "sector_excluded" in match
        assert "sector_excluded_reason" in match
        assert match["sector_excluded"] is False
        assert match["sector_excluded_reason"] is None


# ── signal_type filter (Bug 1 fix) ────────────────────────────────────────────


@pytest.mark.db
class TestSignalTypeFilter:
    """?signal_type= filters leads to only those whose company has that signal type."""

    def _make_company_with_signal(self, db_session, name, ext_id, signal_type_val):
        import uuid as uuid_mod
        from datetime import date
        from app.db.models import (
            Company,
            EvidenceItem,
            LeadCandidate,
            PipelineRun,
            RawSourceEvent,
            Signal,
            SourceRegistry,
            SourceRun,
        )

        # Source registry — create one per call to avoid unique-name collisions.
        source = SourceRegistry(
            id=uuid_mod.uuid4(),
            name=f"test-src-{ext_id}",
            category="government",
            access_method="api",
            status="enabled",
            enabled=True,
            cost_type="free",
            legal_notes="test",
        )
        db_session.add(source)
        db_session.flush()

        # Pipeline run (completed so no single-running uniqueness conflict).
        pipeline_run = PipelineRun(
            id=uuid_mod.uuid4(),
            status="completed",
            trigger="test",
        )
        db_session.add(pipeline_run)
        db_session.flush()

        # Source run tied to pipeline.
        source_run = SourceRun(
            id=uuid_mod.uuid4(),
            pipeline_run_id=pipeline_run.id,
            source_id=source.id,
            status="completed",
        )
        db_session.add(source_run)
        db_session.flush()

        company = Company(
            id=uuid_mod.uuid4(),
            canonical_name=name,
            normalized_name=name.lower(),
            external_id=ext_id,
            country="US",
        )
        db_session.add(company)
        db_session.flush()

        raw_event = RawSourceEvent(
            id=uuid_mod.uuid4(),
            source_id=source.id,
            source_run_id=source_run.id,
            payload={"test": True},
            content_hash=ext_id + "hash",
        )
        db_session.add(raw_event)
        db_session.flush()

        ev = EvidenceItem(
            id=uuid_mod.uuid4(),
            raw_event_id=raw_event.id,
            source_id=source.id,
            company_id=company.id,
            source_url="https://example.com",
            content_hash=ext_id + "evhash",
            claim_supported=signal_type_val,
            confidence_score="0.9",
            freshness_score="0.9",
        )
        db_session.add(ev)
        db_session.flush()

        signal = Signal(
            id=uuid_mod.uuid4(),
            company_id=company.id,
            source_id=source.id,
            evidence_id=ev.id,
            signal_type=signal_type_val,
            signal_date=date(2026, 6, 1),
            signal_strength="strong",
            freshness_score="0.9",
        )
        db_session.add(signal)
        db_session.flush()

        lead = LeadCandidate(
            id=uuid_mod.uuid4(),
            company_id=company.id,
            status="active",
            tier="warm",
            current_score=60,
            sales_status="research",
        )
        db_session.add(lead)
        db_session.flush()
        return company.canonical_name

    def test_signal_type_param_accepted(self, client):
        r = client.get("/api/leads?signal_type=CONTRACT_AWARD")
        assert r.status_code == 200

    def test_signal_type_subcontract_accepted(self, client):
        r = client.get("/api/leads?signal_type=SUBCONTRACT_AWARD")
        assert r.status_code == 200

    def test_signal_type_sba_pif_accepted(self, client):
        r = client.get("/api/leads?signal_type=SBA_LOAN_PIF")
        assert r.status_code == 200

    def test_signal_type_sba_active_accepted(self, client):
        r = client.get("/api/leads?signal_type=SBA_LOAN_ACTIVE")
        assert r.status_code == 200

    @pytest.mark.db
    def test_sba_pif_signal_type_returned_in_list(self, client, db_session):
        """API returns signal_type='SBA_LOAN_PIF' for a lead with an SBA_LOAN_PIF signal."""
        self._make_company_with_signal(
            db_session, "SBA PIF Test Corp", "sbapif00012345", "SBA_LOAN_PIF"
        )
        r = client.get("/api/leads?include_excluded=true")
        assert r.status_code == 200
        items = r.json()["items"]
        match = next((i for i in items if i["company_name"] == "SBA PIF Test Corp"), None)
        assert match is not None, "SBA_LOAN_PIF lead not found in API response"
        assert match["signal_type"] == "SBA_LOAN_PIF", (
            f"Expected signal_type='SBA_LOAN_PIF', got {match['signal_type']!r}"
        )

    @pytest.mark.db
    def test_sba_pif_filter_returns_sba_leads(self, client, db_session):
        """?signal_type=SBA_LOAN_PIF returns leads whose company has SBA_LOAN_PIF signal."""
        self._make_company_with_signal(
            db_session, "SBA PIF Filter Corp", "sbapiffilter123", "SBA_LOAN_PIF"
        )
        r = client.get("/api/leads?signal_type=SBA_LOAN_PIF&include_excluded=true")
        assert r.status_code == 200
        names = [i["company_name"] for i in r.json()["items"]]
        assert "SBA PIF Filter Corp" in names

    @pytest.mark.db
    def test_sba_pif_filter_excludes_contract_leads(self, client, db_session):
        """?signal_type=SBA_LOAN_PIF must not return a lead with only CONTRACT_AWARD signal."""
        self._make_company_with_signal(
            db_session, "Prime No SBA Corp", "primensbaf1234", "CONTRACT_AWARD"
        )
        r = client.get("/api/leads?signal_type=SBA_LOAN_PIF&include_excluded=true")
        assert r.status_code == 200
        names = [i["company_name"] for i in r.json()["items"]]
        assert "Prime No SBA Corp" not in names

    @pytest.mark.db
    def test_subcontract_filter_excludes_contract_leads(self, client, db_session):
        """?signal_type=SUBCONTRACT_AWARD must not return a lead whose only signal is CONTRACT_AWARD."""
        self._make_company_with_signal(
            db_session, "Prime Only Corp", "primeonly001234", "CONTRACT_AWARD"
        )
        r = client.get("/api/leads?signal_type=SUBCONTRACT_AWARD&include_excluded=true")
        assert r.status_code == 200
        names = [i["company_name"] for i in r.json()["items"]]
        assert "Prime Only Corp" not in names

    @pytest.mark.db
    def test_contract_filter_excludes_subcontract_leads(self, client, db_session):
        """?signal_type=CONTRACT_AWARD must not return a lead whose only signal is SUBCONTRACT_AWARD."""
        self._make_company_with_signal(
            db_session, "Sub Only Corp", "subonly0012345", "SUBCONTRACT_AWARD"
        )
        r = client.get("/api/leads?signal_type=CONTRACT_AWARD&include_excluded=true")
        assert r.status_code == 200
        names = [i["company_name"] for i in r.json()["items"]]
        assert "Sub Only Corp" not in names

    @pytest.mark.db
    def test_subcontract_filter_returns_subcontract_leads(self, client, db_session):
        """?signal_type=SUBCONTRACT_AWARD returns a lead whose company has SUBCONTRACT_AWARD signal."""
        self._make_company_with_signal(
            db_session, "Sub Award Corp", "subaward012345", "SUBCONTRACT_AWARD"
        )
        r = client.get("/api/leads?signal_type=SUBCONTRACT_AWARD&include_excluded=true")
        assert r.status_code == 200
        names = [i["company_name"] for i in r.json()["items"]]
        assert "Sub Award Corp" in names


# ── company_naics / awarding_agency in list response (Bug 4 fix) ──────────────


class TestListResponseEnrichedFields:
    """company_naics, company_naics_description, company_state, awarding_agency
    are present in the schema and returned by the API."""

    def test_schema_has_company_naics(self):
        from app.api.schemas import LeadListItemSchema
        assert "company_naics" in LeadListItemSchema.model_fields

    def test_schema_has_company_naics_description(self):
        from app.api.schemas import LeadListItemSchema
        assert "company_naics_description" in LeadListItemSchema.model_fields

    def test_schema_has_company_state(self):
        from app.api.schemas import LeadListItemSchema
        assert "company_state" in LeadListItemSchema.model_fields

    def test_schema_has_awarding_agency(self):
        from app.api.schemas import LeadListItemSchema
        assert "awarding_agency" in LeadListItemSchema.model_fields

    def test_enriched_fields_default_none(self):
        from app.api.schemas import LeadListItemSchema
        item = LeadListItemSchema(
            lead_id="00000000-0000-0000-0000-000000000001",
            company_id="00000000-0000-0000-0000-000000000002",
            company_name="Test Co",
            tier="warm",
            score=60,
            sales_status="research",
            primary_source="usaspending",
            latest_signal_date=None,
            max_award_amount=None,
            is_new_in_run=False,
            created_at="2026-01-01T00:00:00",
            updated_at="2026-01-01T00:00:00",
        )
        assert item.company_naics is None
        assert item.company_naics_description is None
        assert item.company_state is None
        assert item.awarding_agency is None

    @pytest.mark.db
    def test_company_naics_returned_in_list_response(self, client, db_session):
        """Leads list includes company_naics from the Company record."""
        import uuid as uuid_mod
        from app.db.models import Company, LeadCandidate

        company = Company(
            id=uuid_mod.uuid4(),
            canonical_name="NAICS Field Test Co",
            normalized_name="naics field test co",
            external_id="naicstest012345",
            country="US",
            naics_code="541511",
            naics_description="Custom Computer Programming Services",
            state="VA",
        )
        db_session.add(company)
        db_session.flush()

        lead = LeadCandidate(
            id=uuid_mod.uuid4(),
            company_id=company.id,
            status="active",
            sales_status="research",
        )
        db_session.add(lead)
        db_session.flush()

        r = client.get("/api/leads")
        assert r.status_code == 200
        items = r.json()["items"]
        match = next((i for i in items if i["company_name"] == "NAICS Field Test Co"), None)
        assert match is not None, "lead not found in response"
        assert match["company_naics"] == "541511"
        assert match["company_naics_description"] == "Custom Computer Programming Services"
        assert match["company_state"] == "VA"


# ── score bar scaling (Bug 3 fix) ─────────────────────────────────────────────


class TestScoreBarScaling:
    """Score bar width formula produces expected percentages at key values."""

    def _bar_pct(self, score):
        """Replicates the frontend formula: min(100, (score / 73) * 100)."""
        return min(100, ((score if score is not None else 0) / 73) * 100)

    def test_max_score_fills_bar(self):
        assert self._bar_pct(73) == pytest.approx(100.0)

    def test_zero_score_empty_bar(self):
        assert self._bar_pct(0) == pytest.approx(0.0)

    def test_null_score_empty_bar(self):
        assert self._bar_pct(None) == pytest.approx(0.0)

    def test_mid_score_proportional(self):
        pct = self._bar_pct(36)
        assert 48 < pct < 50


# ── DOMESTIC AWARDEES suppression (Bug 2 fix) ─────────────────────────────────


@pytest.mark.db
class TestDomesticAwardeesSuppression:
    """DOMESTIC AWARDEES (UNDISCLOSED) leads must never appear in active lead views."""

    def test_suppressed_lead_absent_from_default_view(self, client, db_session):
        """A lead with sales_status=suppressed / status=archived must not appear
        in the default /api/leads response (which includes active+archived)."""
        import uuid as uuid_mod
        from app.db.models import Company, LeadCandidate

        company = Company(
            id=uuid_mod.uuid4(),
            canonical_name="DOMESTIC AWARDEES (UNDISCLOSED)",
            normalized_name="domestic awardees undisclosed",
            external_id="domesawd0012345",
            country="US",
        )
        db_session.add(company)
        db_session.flush()

        lead = LeadCandidate(
            id=uuid_mod.uuid4(),
            company_id=company.id,
            status="archived",
            tier="archive",
            current_score=10,
            sales_status="suppressed",
        )
        db_session.add(lead)
        db_session.flush()

        r = client.get("/api/leads?include_excluded=true")
        assert r.status_code == 200
        names = [i["company_name"] for i in r.json()["items"]]
        assert "DOMESTIC AWARDEES (UNDISCLOSED)" not in names, (
            "suppressed DOMESTIC AWARDEES must not appear in active lead views"
        )
