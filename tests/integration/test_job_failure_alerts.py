"""Integration tests for Milestone 9 Layer 2: job-failure push alerts.

Real incident this closes: DATA_INGESTION silently failed on every real scheduled run for hours,
invisible unless someone opened the dashboard -- see app.scheduling.tick's own module docstring.
These prove exactly one push fires on the FIRST failure in a streak, and none on a repeat, against
a real database (the streak check queries the real `jobs` table).

Two real gaps a naive version of this test would hit against this repo's shared, never-truncated
Postgres (see conftest.py's own module docstring): (1) DATA_INGESTION's real 4-hour interval means
it may not be "due" at all if another test in this same suite run already succeeded recently --
`_force_data_ingestion_due` bypasses that check. (2) the streak query looks at the single most
recent DATA_INGESTION row by `created_at` REGARDLESS of which test created it -- a FAILED row left
by another test (or a previous test in this file) would spuriously suppress a "first failure"
alert here. `_seed_recent_data_ingestion_job` plants a known, controlled, non-FAILED "previous job"
immediately before each test's own first tick, so the streak check's baseline is deterministic and
order-independent regardless of what else has run in this shared DB.

app.notifications.push_sender.send_push_to_all itself is already covered by
tests/integration/test_api_push.py -- these mock it at the boundary (app.scheduling.tick's own
imported reference) rather than re-testing its internals or needing a real, valid configured VAPID
key (the default is deliberately an invalid placeholder -- see packages/shared/config.py).
"""

from datetime import UTC, datetime

from app.jobs.handlers import data_ingestion
from app.scheduling import tick as tick_module
from packages.database.models.job import Job
from packages.domain.jobs import JobStatus, JobType, TriggerType
from packages.shared.config import get_settings
from tests.integration.conftest import authed_client


def _disable_league_sync(monkeypatch) -> None:
    """Same real gap test_api_internal.py's own tests already close for DATA_INGESTION-focused
    tests -- reports no configured H2H league so run_tick's LEAGUE_SYNC branch is skipped instead
    of making a real, unmocked network call to FPL's public league API."""
    no_league = get_settings().model_copy(update={"fpl_h2h_league_id": None})
    monkeypatch.setattr("app.scheduling.tick.get_settings", lambda: no_league)


def _force_data_ingestion_due(monkeypatch) -> None:
    """DATA_INGESTION_INTERVAL is a real 4 hours, and this shared, never-truncated Postgres may
    already carry a real recent successful run from elsewhere in this same suite run -- forces
    "never run before" for DATA_INGESTION specifically so run_tick deterministically dispatches it,
    independent of what else has run in this shared DB. Delegates to the real function for every
    other job type, so nothing else in run_tick's own dispatch logic is affected."""
    real_fetch = tick_module._fetch_last_successful_run_at

    def _patched(session, job_type):
        if job_type == JobType.DATA_INGESTION:
            return None
        return real_fetch(session, job_type)

    monkeypatch.setattr(tick_module, "_fetch_last_successful_run_at", _patched)


def _seed_recent_data_ingestion_job(db_session, status: JobStatus) -> None:
    """Plants a real DATA_INGESTION job row so the streak-detection query
    (`app.scheduling.tick._previous_job_of_type_failed`) has a known, controlled "previous job" to
    compare against -- see this module's own docstring for why that's necessary against a shared,
    never-truncated Postgres."""
    db_session.add(
        Job(
            job_type=JobType.DATA_INGESTION,
            status=status,
            requested_by="test-seed",
            trigger_type=TriggerType.LOCAL,
            attempt_number=1,
            completed_at=datetime.now(UTC),
        )
    )
    db_session.commit()


def _fail_data_ingestion(monkeypatch) -> None:
    """Kills DATA_INGESTION's handler on purpose. DataIngestionJobRunner's own `except Exception`
    block (app/jobs/handlers/data_ingestion.py) turns this into a real JobStatus.FAILED row --
    no second layer of mocking needed for the job to reach a genuine terminal FAILED state."""

    def _raise(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(data_ingestion, "run_ingestion", _raise)


def _capture_alerts(monkeypatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "app.scheduling.tick.send_push_to_all",
        lambda session, title, body, url=None: calls.append((title, body)) or 0,
    )
    return calls


def test_first_data_ingestion_failure_sends_exactly_one_alert(db_session, monkeypatch):
    _disable_league_sync(monkeypatch)
    _force_data_ingestion_due(monkeypatch)
    _fail_data_ingestion(monkeypatch)
    _seed_recent_data_ingestion_job(db_session, JobStatus.SUCCEEDED)
    calls = _capture_alerts(monkeypatch)
    client = authed_client()

    response = client.post("/internal/scheduler-tick")

    assert response.status_code == 200
    failed_jobs = (
        db_session.query(Job)
        .filter(Job.job_type == JobType.DATA_INGESTION, Job.status == JobStatus.FAILED)
        .count()
    )
    assert failed_jobs >= 1
    assert len(calls) == 1
    assert calls[0][0] == f"{JobType.DATA_INGESTION.value} failed"


def test_second_consecutive_data_ingestion_failure_sends_no_further_alert(db_session, monkeypatch):
    _disable_league_sync(monkeypatch)
    _force_data_ingestion_due(monkeypatch)
    _fail_data_ingestion(monkeypatch)
    _seed_recent_data_ingestion_job(db_session, JobStatus.SUCCEEDED)
    calls = _capture_alerts(monkeypatch)
    client = authed_client()

    client.post("/internal/scheduler-tick")
    # A failed run never counts as a "last successful run" (see is_interval_due's own real
    # behavioral note in app.scheduling.tick's module docstring), so DATA_INGESTION is retried on
    # the very next tick rather than waiting out the full interval -- this proves the second,
    # still-failing attempt sends no second alert.
    client.post("/internal/scheduler-tick")

    assert len(calls) == 1
