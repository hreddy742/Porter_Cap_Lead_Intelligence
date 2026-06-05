"""
Schema tests — run these after Week 1 setup.

Tests:
1. Migration round-trip (upgrade → downgrade → upgrade)
2. Append-only: UPDATE and DELETE on review_decisions must RAISE
3. One-active-candidate constraint
4. One-active-scoring-config constraint

Run with: pytest tests/test_schema.py -v
Requires: Docker running (testcontainers starts a Postgres instance)
"""

import uuid

import pytest
from sqlalchemy import text


# ── These tests need a real Postgres DB ───────────────────────────────────────

@pytest.mark.db
def test_migration_round_trip(postgres_container):
    """Alembic upgrade → downgrade → upgrade must all succeed cleanly."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine

    engine = create_engine(postgres_container.get_connection_url())
    cfg = Config("alembic.ini")
    cfg.set_main_option(
        "sqlalchemy.url",
        engine.url.render_as_string(hide_password=False),
    )

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")  # Must succeed a second time

    engine.dispose()


@pytest.mark.db
def test_review_decisions_update_raises(db_with_schema):
    """UPDATE on review_decisions must RAISE EXCEPTION — not silently no-op."""
    from sqlalchemy.exc import DBAPIError

    with db_with_schema.connect() as conn:
        company_id = str(uuid.uuid4())
        candidate_id = str(uuid.uuid4())
        decision_id = str(uuid.uuid4())

        conn.execute(
            text("""
                INSERT INTO companies (id, canonical_name, normalized_name)
                VALUES (:id, 'Test Company Inc', 'test')
            """),
            {"id": company_id},
        )

        conn.execute(
            text("""
                INSERT INTO lead_candidates (id, company_id, status, sales_status)
                VALUES (:id, :company_id, 'active', 'research')
            """),
            {"id": candidate_id, "company_id": company_id},
        )

        conn.execute(
            text("""
                INSERT INTO review_decisions (id, lead_candidate_id, reviewer_id, action)
                VALUES (:id, :candidate_id, 'test@example.com', 'approve')
            """),
            {"id": decision_id, "candidate_id": candidate_id},
        )

        conn.commit()

        with pytest.raises(DBAPIError, match="append-only"):
            conn.execute(
                text("""
                    UPDATE review_decisions
                    SET action = 'reject'
                    WHERE id = :id
                """),
                {"id": decision_id},
            )
            conn.commit()


@pytest.mark.db
def test_review_decisions_delete_raises(db_with_schema):
    """DELETE on review_decisions must RAISE EXCEPTION."""
    from sqlalchemy.exc import DBAPIError

    with db_with_schema.connect() as conn:
        company_id = str(uuid.uuid4())
        candidate_id = str(uuid.uuid4())
        decision_id = str(uuid.uuid4())

        conn.execute(
            text("""
                INSERT INTO companies (id, canonical_name, normalized_name)
                VALUES (:id, 'Test Company 2', 'test 2')
            """),
            {"id": company_id},
        )

        conn.execute(
            text("""
                INSERT INTO lead_candidates (id, company_id, status, sales_status)
                VALUES (:id, :company_id, 'active', 'research')
            """),
            {"id": candidate_id, "company_id": company_id},
        )

        conn.execute(
            text("""
                INSERT INTO review_decisions (id, lead_candidate_id, reviewer_id, action)
                VALUES (:id, :candidate_id, 'test@example.com', 'approve')
            """),
            {"id": decision_id, "candidate_id": candidate_id},
        )

        conn.commit()

        with pytest.raises(DBAPIError, match="append-only"):
            conn.execute(
                text("""
                    DELETE FROM review_decisions
                    WHERE id = :id
                """),
                {"id": decision_id},
            )
            conn.commit()


@pytest.mark.db
def test_one_active_candidate_per_company(db_with_schema):
    """Partial unique index prevents two active lead_candidates for the same company."""
    from sqlalchemy.exc import IntegrityError

    with db_with_schema.connect() as conn:
        company_id = str(uuid.uuid4())

        conn.execute(
            text("""
                INSERT INTO companies (id, canonical_name, normalized_name)
                VALUES (:id, 'One Active Test Corp', 'one active test')
            """),
            {"id": company_id},
        )

        conn.execute(
            text("""
                INSERT INTO lead_candidates (id, company_id, status, sales_status)
                VALUES (:id, :company_id, 'active', 'research')
            """),
            {"id": str(uuid.uuid4()), "company_id": company_id},
        )

        conn.commit()

        with pytest.raises(IntegrityError):
            conn.execute(
                text("""
                    INSERT INTO lead_candidates (id, company_id, status, sales_status)
                    VALUES (:id, :company_id, 'active', 'research')
                """),
                {"id": str(uuid.uuid4()), "company_id": company_id},
            )
            conn.commit()


@pytest.mark.db
def test_chk_score_has_evidence_blocks_empty_array(db_with_schema):
    """
    SHIP-BLOCKER FIX: cardinality([]) = 0, so the check correctly rejects
    a non-archived score with empty evidence_ids.
    """
    from sqlalchemy.exc import IntegrityError

    with db_with_schema.connect() as conn:
        company_id = str(uuid.uuid4())
        candidate_id = str(uuid.uuid4())
        config_id = str(uuid.uuid4())

        conn.execute(
            text("""
                INSERT INTO companies (id, canonical_name, normalized_name)
                VALUES (:id, 'Score Test Corp', 'score test')
            """),
            {"id": company_id},
        )

        conn.execute(
            text("""
                INSERT INTO lead_candidates (id, company_id, status, sales_status)
                VALUES (:id, :company_id, 'active', 'research')
            """),
            {"id": candidate_id, "company_id": company_id},
        )

        conn.execute(
            text("""
                INSERT INTO scoring_configs (id, version_label, config, config_hash, active)
                VALUES (:id, 'test-v1', '{}', :hash, false)
            """),
            {"id": config_id, "hash": "abc123"},
        )

        conn.commit()

        with pytest.raises(IntegrityError, match="chk_score_has_evidence"):
            conn.execute(
                text("""
                    INSERT INTO lead_scores
                        (
                            id,
                            lead_candidate_id,
                            scoring_config_id,
                            config_hash,
                            total_score,
                            tier,
                            component_breakdown,
                            evidence_ids,
                            gate_result
                        )
                    VALUES
                        (
                            :id,
                            :candidate_id,
                            :config_id,
                            'abc123',
                            75,
                            'hot',
                            '{}',
                            ARRAY[]::uuid[],
                            'passed'
                        )
                """),
                {
                    "id": str(uuid.uuid4()),
                    "candidate_id": candidate_id,
                    "config_id": config_id,
                },
            )
            conn.commit()