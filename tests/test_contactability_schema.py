"""
Schema tests for company_contactability and contactability_runs tables.

Requires Docker running (testcontainers).
Run with: pytest tests/test_contactability_schema.py -v -m db

Tests:
  1. contactability_runs table exists after migration
  2. company_contactability table exists after migration
  3. contactability_status CHECK constraint rejects invalid values
  4. contactability_status CHECK allows all four valid values
  5. company_id UNIQUE constraint rejects duplicate company rows
  6. website_confidence CHECK rejects out-of-range values
  7. contactability_score CHECK rejects out-of-range values
  8. migration round-trip still passes with new tables
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


@pytest.mark.db
def test_contactability_runs_table_exists(db_with_schema):
    with db_with_schema.connect() as conn:
        result = conn.execute(
            text("""
                SELECT COUNT(*) FROM information_schema.tables
                WHERE table_name = 'contactability_runs'
            """)
        ).scalar()
    assert result == 1


@pytest.mark.db
def test_company_contactability_table_exists(db_with_schema):
    with db_with_schema.connect() as conn:
        result = conn.execute(
            text("""
                SELECT COUNT(*) FROM information_schema.tables
                WHERE table_name = 'company_contactability'
            """)
        ).scalar()
    assert result == 1


@pytest.mark.db
def test_contactability_status_invalid_value_rejected(db_session):
    from sqlalchemy.exc import DBAPIError

    company_id = str(uuid.uuid4())
    db_session.execute(
        text("""
            INSERT INTO companies (id, canonical_name, normalized_name)
            VALUES (:id, 'Test Corp', 'test corp')
        """),
        {"id": company_id},
    )

    with pytest.raises(DBAPIError):
        db_session.execute(
            text("""
                INSERT INTO company_contactability
                    (id, company_id, contactability_status, last_checked_at)
                VALUES (:id, :company_id, 'invalid_value', NOW())
            """),
            {"id": str(uuid.uuid4()), "company_id": company_id},
        )
        db_session.flush()


@pytest.mark.db
@pytest.mark.parametrize("status", [
    "contactable",
    "partially_contactable",
    "not_contactable",
    "needs_paid_enrichment",
])
def test_contactability_status_valid_values_accepted(db_session, status):
    company_id = str(uuid.uuid4())
    db_session.execute(
        text("""
            INSERT INTO companies (id, canonical_name, normalized_name)
            VALUES (:id, 'Test Corp', 'test corp')
        """),
        {"id": company_id},
    )
    db_session.execute(
        text("""
            INSERT INTO company_contactability
                (id, company_id, contactability_status, last_checked_at)
            VALUES (:id, :company_id, :status, NOW())
        """),
        {"id": str(uuid.uuid4()), "company_id": company_id, "status": status},
    )
    db_session.flush()


@pytest.mark.db
def test_company_id_unique_constraint_enforced(db_session):
    from sqlalchemy.exc import DBAPIError

    company_id = str(uuid.uuid4())
    db_session.execute(
        text("""
            INSERT INTO companies (id, canonical_name, normalized_name)
            VALUES (:id, 'Test Corp', 'test corp')
        """),
        {"id": company_id},
    )
    db_session.execute(
        text("""
            INSERT INTO company_contactability
                (id, company_id, contactability_status, last_checked_at)
            VALUES (:id, :company_id, 'not_contactable', NOW())
        """),
        {"id": str(uuid.uuid4()), "company_id": company_id},
    )
    db_session.flush()

    with pytest.raises(DBAPIError):
        db_session.execute(
            text("""
                INSERT INTO company_contactability
                    (id, company_id, contactability_status, last_checked_at)
                VALUES (:id, :company_id, 'not_contactable', NOW())
            """),
            {"id": str(uuid.uuid4()), "company_id": company_id},
        )
        db_session.flush()


@pytest.mark.db
def test_website_confidence_check_rejects_out_of_range(db_session):
    from sqlalchemy.exc import DBAPIError

    company_id = str(uuid.uuid4())
    db_session.execute(
        text("""
            INSERT INTO companies (id, canonical_name, normalized_name)
            VALUES (:id, 'Test Corp', 'test corp')
        """),
        {"id": company_id},
    )
    with pytest.raises(DBAPIError):
        db_session.execute(
            text("""
                INSERT INTO company_contactability
                    (id, company_id, contactability_status, website_confidence, last_checked_at)
                VALUES (:id, :company_id, 'not_contactable', 1.5, NOW())
            """),
            {"id": str(uuid.uuid4()), "company_id": company_id},
        )
        db_session.flush()


@pytest.mark.db
def test_contactability_score_check_rejects_out_of_range(db_session):
    from sqlalchemy.exc import DBAPIError

    company_id = str(uuid.uuid4())
    db_session.execute(
        text("""
            INSERT INTO companies (id, canonical_name, normalized_name)
            VALUES (:id, 'Test Corp', 'test corp')
        """),
        {"id": company_id},
    )
    with pytest.raises(DBAPIError):
        db_session.execute(
            text("""
                INSERT INTO company_contactability
                    (id, company_id, contactability_status, contactability_score, last_checked_at)
                VALUES (:id, :company_id, 'not_contactable', 11, NOW())
            """),
            {"id": str(uuid.uuid4()), "company_id": company_id},
        )
        db_session.flush()


@pytest.mark.db
def test_migration_round_trip_includes_contactability(postgres_container):
    """upgrade → downgrade → upgrade must succeed — verifies 002 migration."""
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
    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        for table in ("contactability_runs", "company_contactability"):
            count = conn.execute(
                text(f"SELECT COUNT(*) FROM information_schema.tables WHERE table_name = '{table}'")
            ).scalar()
            assert count == 1, f"Table {table!r} missing after round-trip migration"

    engine.dispose()
