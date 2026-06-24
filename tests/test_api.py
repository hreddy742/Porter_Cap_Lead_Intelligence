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
        for tier in ("hot", "warm", "cold"):
            r = client.get(f"/api/leads?tier={tier}")
            assert r.status_code == 200

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
