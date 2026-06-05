"""
Database session factory.
All application code should use get_session() to get a Session.
"""

import os
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://porter:localdev@localhost:5432/porter_leads",
)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,      # verify connections are alive
    pool_size=5,
    max_overflow=10,
)

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_session() -> Generator[Session, None, None]:
    """
    Yield a database session.
    Usage:
        with get_session() as session:
            session.add(...)
            session.commit()
    """
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
