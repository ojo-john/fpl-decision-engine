"""Shared fixtures for integration tests.

These tests require a real, reachable Postgres database (the same one
Alembic migrations target) — via `docker compose up -d postgres` locally, or
a Postgres service container in CI. Docker is not installed on the machine
this repo was originally scaffolded on, so locally these tests are expected
to skip cleanly (not error) via the `require_database` fixture below. They
run for real in CI, where a Postgres service container is always available,
and will run locally too once Docker is installed.
"""

import copy
import itertools
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from packages.database.session import SessionLocal, engine
from packages.domain.ingestion import EndpointHealth
from packages.fpl_client import FetchResult, FPLClient
from packages.fpl_client.fingerprint import schema_signature, sha256_hex


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


# --- FPL API fixture-data helpers (no live network access) ------------------

FPL_API_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "fpl_api"

# Seeded from wall-clock time (not a fixed constant) so re-running the suite
# against the same persistent, non-ephemeral local Postgres across separate
# process invocations doesn't collide with a season label left over from a
# previous run — each pytest session gets its own practically-unique block of
# synthetic season years.
_season_year_counter = itertools.count(2100 + (int(time.time()) % 5000))


def load_bootstrap_json() -> dict:
    return json.loads((FPL_API_FIXTURES_DIR / "bootstrap_static_sample.json").read_text())


def load_fixtures_json() -> list:
    return json.loads((FPL_API_FIXTURES_DIR / "fixtures_sample.json").read_text())


def unique_bootstrap_json(base: dict | None = None) -> dict:
    """Return a bootstrap-static payload whose season label is fresh for this test run.

    Every ingestion integration test works against the same persistent local
    Postgres, so without this, two tests (or two runs of the suite) would
    collide on the same "2025-26" season derived from the fixture's real
    deadline dates. Shifting every event's deadline year to a fresh synthetic
    value keeps each test's rows fully isolated without needing to truncate
    tables between tests.
    """
    bootstrap = copy.deepcopy(base if base is not None else load_bootstrap_json())
    year = next(_season_year_counter)
    for event in bootstrap["events"]:
        event["deadline_time"] = f"{year}{event['deadline_time'][4:]}"
    return bootstrap


def make_fetch_result(endpoint: str, body, retrieved_at: datetime | None = None) -> FetchResult:
    """Build a FetchResult as if it came from a real, healthy FPLClient fetch."""
    retrieved_at = retrieved_at or datetime.now(UTC)
    raw_bytes = json.dumps(body).encode()
    return FetchResult(
        endpoint=endpoint,
        request_url=f"https://fantasy.premierleague.com/api/{endpoint}/",
        request_params=None,
        retrieved_at=retrieved_at,
        http_status=200,
        attempt_count=1,
        latency_ms=10,
        health_status=EndpointHealth.HEALTHY,
        checksum_sha256=sha256_hex(raw_bytes),
        schema_signature=schema_signature(body),
        raw_body=body,
        error_message=None,
    )


@pytest.fixture
def patched_fpl_client(monkeypatch):
    """Monkeypatch FPLClient so job handlers never hit the real network in tests.

    Returns a mutable ``state`` dict (``bootstrap``/``fixtures``/``event_status``)
    that a test can edit *before* calling the job handler again, to simulate a
    rules change or a player transfer between two ingestion runs.
    """
    state = {
        "bootstrap": unique_bootstrap_json(),
        "fixtures": load_fixtures_json(),
        "event_status": {"leagues": "updated"},
    }

    def fetch_bootstrap_static(self):
        return make_fetch_result("bootstrap_static", state["bootstrap"])

    def fetch_fixtures(self, event=None):
        endpoint = "fixtures_by_event" if event is not None else "fixtures"
        return make_fetch_result(endpoint, state["fixtures"])

    def fetch_event_status(self):
        return make_fetch_result("event_status", state["event_status"])

    monkeypatch.setattr(FPLClient, "fetch_bootstrap_static", fetch_bootstrap_static)
    monkeypatch.setattr(FPLClient, "fetch_fixtures", fetch_fixtures)
    monkeypatch.setattr(FPLClient, "fetch_event_status", fetch_event_status)

    return state
