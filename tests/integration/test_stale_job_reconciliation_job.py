"""Integration tests for STALE_JOB_RECONCILIATION, against a real database.

Real incident this closes (see app.jobs.handlers.stale_job_reconciliation's own module
docstring): a process running a job can be killed externally (a Render redeploy, an OOM,
resource contention) without ever running app.jobs.service.execute_job's own except/finally
block, leaving both the jobs row AND its linked run-header row (e.g. league_sync_runs) stuck in
RUNNING forever. These prove the reconciliation actually finalizes both rows for a genuinely
stale job, leaves a still-legitimately-in-progress job alone, and finalizes the jobs row even for
a job type with no run-header table of its own.

Shared, never-truncated Postgres (see conftest.py's own module docstring) -- this handler's own
select(Job).where(Job.status == RUNNING) query is deliberately unscoped (that's the real, correct
production behavior), so assertions below check the EXACT rows this test created by their own
ids, never a bare count, to stay correct regardless of whatever else the shared DB happens to
contain.
"""

import uuid
from datetime import UTC, datetime, timedelta

from app.jobs.handlers.stale_job_reconciliation import ORPHANED_ERROR_CODE, stale_threshold_for
from app.jobs.service import execute_job
from packages.database.models.job import Job
from packages.database.models.league_sync_run import LeagueSyncRun
from packages.database.models.season import Season
from packages.domain.h2h import LeagueSyncRunStatus
from packages.domain.jobs import JobStatus, JobType, TriggerType


def _make_season(db_session) -> uuid.UUID:
    season = Season(label=f"sjr-{uuid.uuid4().hex[:12]}", is_current=False)
    db_session.add(season)
    db_session.commit()
    db_session.refresh(season)
    return season.id


def _make_running_job(db_session, job_type: JobType, started_at: datetime) -> Job:
    job = Job(
        job_type=job_type,
        requested_by="test",
        trigger_type=TriggerType.LOCAL,
        status=JobStatus.RUNNING,
        started_at=started_at,
        attempt_number=1,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    return job


def test_reconciles_a_stale_run_and_its_linked_run_header_row(db_session):
    threshold = stale_threshold_for(JobType.LEAGUE_SYNC)
    stale_started_at = datetime.now(UTC) - threshold - timedelta(minutes=1)
    job = _make_running_job(db_session, JobType.LEAGUE_SYNC, stale_started_at)
    season_id = _make_season(db_session)
    run = LeagueSyncRun(
        job_id=job.id,
        season_id=season_id,
        run_status=LeagueSyncRunStatus.RUNNING,
        started_at=datetime.now(UTC),
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)

    result = execute_job(
        db_session,
        job_type=JobType.STALE_JOB_RECONCILIATION,
        requested_by="test",
        trigger_type=TriggerType.LOCAL,
    )

    assert result.status == JobStatus.SUCCEEDED
    db_session.refresh(job)
    assert job.status == JobStatus.FAILED
    assert job.error_code == ORPHANED_ERROR_CODE
    db_session.refresh(run)
    assert run.run_status == LeagueSyncRunStatus.FAILED
    assert run.error_code == ORPHANED_ERROR_CODE
    assert run.completed_at is not None


def test_leaves_a_run_still_within_its_threshold_untouched(db_session):
    recent_started_at = datetime.now(UTC) - timedelta(minutes=1)
    job = _make_running_job(db_session, JobType.LEAGUE_SYNC, recent_started_at)
    season_id = _make_season(db_session)
    run = LeagueSyncRun(
        job_id=job.id,
        season_id=season_id,
        run_status=LeagueSyncRunStatus.RUNNING,
        started_at=datetime.now(UTC),
    )
    db_session.add(run)
    db_session.commit()

    execute_job(
        db_session,
        job_type=JobType.STALE_JOB_RECONCILIATION,
        requested_by="test",
        trigger_type=TriggerType.LOCAL,
    )

    db_session.refresh(job)
    assert job.status == JobStatus.RUNNING
    db_session.refresh(run)
    assert run.run_status == LeagueSyncRunStatus.RUNNING

    # A lingering RUNNING row is exactly the state a "most recent run" query elsewhere in the
    # shared, never-truncated test DB could pick up -- clean up now that the assertions above
    # already captured what this test needed to prove. No ORM relationship() is configured
    # between Job and LeagueSyncRun (only a raw job_id column), so the FK-respecting order (run
    # before job) has to be done explicitly, each in its own commit.
    db_session.delete(run)
    db_session.commit()
    db_session.delete(job)
    db_session.commit()


def test_reconciles_a_job_type_with_no_run_header_table(db_session):
    """DEADLINE_REMINDER writes no run-header row of its own -- only the jobs row itself should
    be finalized, and that alone is still a real, complete fix (see this handler's own
    _RunHeaderMapping docstring for why False here is expected, not a bug)."""
    threshold = stale_threshold_for(JobType.DEADLINE_REMINDER)
    stale_started_at = datetime.now(UTC) - threshold - timedelta(minutes=1)
    job = _make_running_job(db_session, JobType.DEADLINE_REMINDER, stale_started_at)

    execute_job(
        db_session,
        job_type=JobType.STALE_JOB_RECONCILIATION,
        requested_by="test",
        trigger_type=TriggerType.LOCAL,
    )

    db_session.refresh(job)
    assert job.status == JobStatus.FAILED
    assert job.error_code == ORPHANED_ERROR_CODE
