"""
Shared test fixtures.

testcontainers spins up a real Postgres instance for DB tests.
Unit tests (like normalize) don't need it — they run without Docker.
"""

import pytest


# ─── DB fixtures (only needed for schema/integration tests) ────────────────────

@pytest.fixture(scope="session")
def postgres_container():
    """Spin up a disposable Postgres container for the test session."""
    try:
        from testcontainers.postgres import PostgresContainer

        with PostgresContainer(
            "postgres:16",
            username="test",
            password="test",
            dbname="test",
        ) as pg:
            yield pg
    except ImportError:
        pytest.skip("testcontainers not installed — skipping DB tests")


@pytest.fixture(scope="session")
def db_engine(postgres_container):
    """Create engine connected to the test container."""
    from sqlalchemy import create_engine

    engine = create_engine(postgres_container.get_connection_url())
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def db_with_schema(db_engine):
    """Run all migrations against the test DB; yield engine with schema applied."""
    from alembic import command
    from alembic.config import Config

    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option(
        "sqlalchemy.url",
        db_engine.url.render_as_string(hide_password=False),
    )
    command.upgrade(alembic_cfg, "head")

    yield db_engine

    # Teardown: downgrade back to base (tests the downgrade path too)
    command.downgrade(alembic_cfg, "base")


@pytest.fixture
def db_session(db_with_schema):
    """Provide a transactional session that rolls back after each test."""
    from sqlalchemy.orm import Session

    with Session(db_with_schema) as session:
        with session.begin():
            yield session
            session.rollback()
