"""Shared fixtures for integration tests.

These tests require a real, reachable Postgres database (the same one
Alembic migrations target) — via `docker compose up -d postgres` locally, or
a Postgres service container in CI. Docker is not installed on the machine
this repo was originally scaffolded on, so locally these tests are expected
to skip cleanly (not error) via the `require_database` fixture below. They
run for real in CI, where a Postgres service container is always available,
and will run locally too once Docker is installed.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from packages.database.session import SessionLocal, engine


def _database_reachable() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except SQLAlchemyError:
        return False


@pytest.fixture(scope="session")
def require_database():
    """Skip the test cleanly if Postgres isn't reachable."""
    if not _database_reachable():
        pytest.skip("Postgres not reachable — skipping integration tests")


@pytest.fixture
def db_session(require_database):
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
