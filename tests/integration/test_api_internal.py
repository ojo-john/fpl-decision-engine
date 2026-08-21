"""Integration tests for POST /internal/scheduler-tick against a real database.

Proves the real end-to-end wiring (auth, real DB-backed "last run" derivation via
app.scheduling.tick.run_tick) -- the pure decision-function logic itself is already covered in
depth by tests/unit/test_scheduling_tick.py.
"""

from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.database.models.job import Job
from packages.domain.jobs import JobStatus, JobType
from packages.shared.config import get_settings
from tests.integration.conftest import authed_client


def _disable_league_sync(monkeypatch) -> None:
    """Report no configured H2H league for this test only, so run_tick's own LEAGUE_SYNC branch
    is skipped (see app.scheduling.tick.run_tick) instead of making a real, unmocked network call
    to FPL's public league API."""
    no_league = get_settings().model_copy(update={"fpl_h2h_league_id": None})
    monkeypatch.setattr("app.scheduling.tick.get_settings", lambda: no_league)


def test_scheduler_tick_without_authorization_header_returns_401():
    client = TestClient(create_app())

    response = client.post("/internal/scheduler-tick")

    assert response.status_code == 401


def test_scheduler_tick_triggers_data_ingestion_when_never_run_before(
    db_session, patched_fpl_client_optimization, monkeypatch
):
    # LEAGUE_SYNC's own real HTTP client isn't patched by patched_fpl_client_optimization (a
    # different, unmocked network call) -- these tests are about DATA_INGESTION scheduling
    # specifically, so disable league-sync scheduling entirely by reporting no configured
    # league, avoiding a real, non-deterministic network call in this test.
    _disable_league_sync(monkeypatch)
    client = authed_client()

    response = client.post("/internal/scheduler-tick")

    assert response.status_code == 200
    assert response.json() == {"status": "tick_completed"}

    succeeded_data_ingestion_jobs = (
        db_session.query(Job)
        .filter(Job.job_type == JobType.DATA_INGESTION, Job.status == JobStatus.SUCCEEDED)
        .count()
    )
    assert succeeded_data_ingestion_jobs >= 1


def test_scheduler_tick_does_not_re_trigger_data_ingestion_immediately_after_success(
    db_session, patched_fpl_client_optimization, monkeypatch
):
    _disable_league_sync(monkeypatch)
    client = authed_client()

    client.post("/internal/scheduler-tick")
    first_count = (
        db_session.query(Job)
        .filter(Job.job_type == JobType.DATA_INGESTION, Job.status == JobStatus.SUCCEEDED)
        .count()
    )

    # A second, separate HTTP call -- proves the "last run" state genuinely persists in the real
    # database across independent requests (no shared in-memory state between them), not just
    # within a single process's own long-lived loop.
    response = client.post("/internal/scheduler-tick")
    second_count = (
        db_session.query(Job)
        .filter(Job.job_type == JobType.DATA_INGESTION, Job.status == JobStatus.SUCCEEDED)
        .count()
    )

    assert response.status_code == 200
    assert second_count == first_count
