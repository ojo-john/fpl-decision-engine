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


def _disable_league_and_manager_sync(monkeypatch) -> None:
    """Like _disable_league_sync, but also reports no configured manager id, so run_tick's
    MANAGER_SYNC branch is skipped too -- for tests that care about neither and would otherwise
    make real, unmocked network calls for both."""
    neither = get_settings().model_copy(update={"fpl_h2h_league_id": None, "fpl_manager_id": None})
    monkeypatch.setattr("app.scheduling.tick.get_settings", lambda: neither)


def test_scheduler_tick_without_authorization_header_returns_401():
    client = TestClient(create_app())

    response = client.post("/internal/scheduler-tick")

    assert response.status_code == 401


def test_scheduler_tick_triggers_data_ingestion_when_never_run_before(
    db_session, patched_fpl_client_optimization, monkeypatch
):
    # LEAGUE_SYNC/MANAGER_SYNC's own real HTTP clients aren't patched by
    # patched_fpl_client_optimization (different, unmocked network calls) -- these tests are
    # about DATA_INGESTION scheduling specifically, so disable both by reporting no configured
    # league/manager, avoiding real, non-deterministic network calls in this test. (.env.test
    # sets FPL_MANAGER_ID for other tests' own use, so MANAGER_SYNC would otherwise be "due"
    # here too, exactly like LEAGUE_SYNC already was.)
    _disable_league_and_manager_sync(monkeypatch)
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
    _disable_league_and_manager_sync(monkeypatch)
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


def test_scheduler_tick_triggers_manager_sync_when_manager_configured(
    db_session, patched_fpl_client_optimization, patched_fpl_client_manager, monkeypatch
):
    """Real gap this closes (found live, 2026-09-07): MANAGER_SYNC previously had no automatic
    trigger anywhere -- the user's own squad/bank state could go stale indefinitely with nothing
    to refresh it. Mirrors LEAGUE_SYNC's own config-gated shape exactly (see run_tick).

    Two ticks, not one: MANAGER_SYNC runs before DATA_INGESTION within a single run_tick pass, so
    on a database with no current season yet, the first tick's MANAGER_SYNC attempt genuinely
    fails with NO_CURRENT_SEASON before that same tick's own DATA_INGESTION creates one -- exactly
    like a real fresh deployment's first-ever tick would. The second tick retries (a FAILED run
    never counts as "last successful", see is_interval_due) against a database that now has one.
    """
    _disable_league_sync(monkeypatch)
    configured = get_settings().model_copy(update={"fpl_manager_id": "4923902"})
    monkeypatch.setattr("app.scheduling.tick.get_settings", lambda: configured)
    client = authed_client()

    client.post("/internal/scheduler-tick")
    response = client.post("/internal/scheduler-tick")

    assert response.status_code == 200
    succeeded_manager_sync_jobs = (
        db_session.query(Job)
        .filter(Job.job_type == JobType.MANAGER_SYNC, Job.status == JobStatus.SUCCEEDED)
        .count()
    )
    assert succeeded_manager_sync_jobs >= 1


def test_scheduler_tick_skips_manager_sync_when_no_manager_configured(
    db_session, patched_fpl_client_optimization, monkeypatch
):
    """The other half of the gate above: with no fpl_manager_id configured (this deployment's own
    real production state until Render's dashboard is set -- see ARCHITECTURE.md), MANAGER_SYNC
    must never fire and hit FPL's real manager API unmocked."""
    _disable_league_and_manager_sync(monkeypatch)
    client = authed_client()
    manager_sync_jobs_before = db_session.query(Job).filter(Job.job_type == JobType.MANAGER_SYNC).count()

    response = client.post("/internal/scheduler-tick")

    assert response.status_code == 200
    manager_sync_jobs_after = db_session.query(Job).filter(Job.job_type == JobType.MANAGER_SYNC).count()
    assert manager_sync_jobs_after == manager_sync_jobs_before
