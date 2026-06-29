"""
Shared test fixtures.

testcontainers spins up a real Postgres instance for DB tests.
Unit tests (like normalize) don't need it — they run without Docker.
"""

import os

import pytest


def pytest_configure(config):
    """Hard safety check — abort immediately if DATABASE_URL points to the live DB.

    The test suite runs alembic downgrade('base') which drops every table.
    If DATABASE_URL is set to localhost/porter_leads, that downgrade runs
    against the production database and destroys all data.  This check fires
    before any fixture or test executes so the mistake is caught at startup.

    To run tests safely: unset DATABASE_URL first.
      PowerShell : Remove-Item Env:DATABASE_URL
      bash/zsh   : unset DATABASE_URL
    """
    db_url = os.environ.get("DATABASE_URL", "")
    if "porter_leads" in db_url and "localhost" in db_url:
        pytest.exit(
            reason=(
                "\n"
                "SAFETY BLOCK: Tests are configured to run against the live database\n"
                "(DATABASE_URL contains 'localhost' and 'porter_leads').\n"
                "Running the test suite would execute alembic downgrade('base'),\n"
                "dropping every table and destroying all production data.\n"
                "\n"
                "Unset DATABASE_URL before running pytest:\n"
                "  PowerShell : Remove-Item Env:DATABASE_URL\n"
                "  bash/zsh   : unset DATABASE_URL\n"
            ),
            returncode=1,
        )


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
    """Run all migrations against the test DB; yield engine with schema applied.

    Defensively downgrades to base before upgrading to head because
    test_migration_round_trip operates on the same shared postgres_container
    (upgrade→downgrade→upgrade) and can leave the DB in any state depending
    on test ordering.  Starting from a forced base→head guarantees schema tests
    always see a fully-migrated database regardless of prior state.
    """
    import os
    from alembic import command
    from alembic.config import Config

    test_url = db_engine.url.render_as_string(hide_password=False)

    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", test_url)

    # alembic/env.py re-reads DATABASE_URL and will clobber our test URL if
    # that env var points elsewhere.  Pin it to the testcontainer for the
    # duration of setup and teardown.
    _prev = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = test_url

    try:
        # Always reset to a known-clean baseline before schema tests rely on tables.
        command.downgrade(alembic_cfg, "base")
        command.upgrade(alembic_cfg, "head")
        # Force fresh connections so any pooled pre-migration connections are
        # replaced and can see the newly created tables.
        db_engine.dispose()
    finally:
        if _prev is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = _prev

    yield db_engine

    # Teardown: downgrade back to base (tests the downgrade path too)
    os.environ["DATABASE_URL"] = test_url
    try:
        command.downgrade(alembic_cfg, "base")
    finally:
        if _prev is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = _prev


@pytest.fixture
def db_session(db_with_schema):
    """Provide a transactional session that rolls back after each test.

    Binds the session to a dedicated connection and wraps everything in an
    outer transaction that is rolled back at the end — regardless of whether
    the endpoint code called db.commit() during the test.

    join_transaction_mode="create_savepoint" means any commit() call inside
    the test (e.g. from submit_lead_review) releases a SAVEPOINT rather than
    committing the outer connection transaction, so the outer rollback still
    undoes all changes made during the test.
    """
    from sqlalchemy.orm import Session

    with db_with_schema.connect() as conn:
        with conn.begin() as outer_txn:
            with Session(
                conn, join_transaction_mode="create_savepoint"
            ) as session:
                yield session
            outer_txn.rollback()
