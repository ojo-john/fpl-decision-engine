"""Integration tests for the SYSTEM_HEALTH job end-to-end through execute_job.

Requires a running Postgres with migrations applied (see conftest.py).
"""

from datetime import UTC, datetime

from app.jobs.handlers import system_health as system_health_module
from app.jobs.service import execute_job
from packages.database.health import HealthCheckResult
from packages.domain.jobs import JobStatus, JobType, TriggerType


def test_system_health_job_succeeds_against_real_db(db_session):
    job = execute_job(
        db_session,
        job_type=JobType.SYSTEM_HEALTH,
        trigger_type=TriggerType.LOCAL,
    )
    assert job.status == JobStatus.SUCCEEDED
    assert job.error_code is None
    assert job.output_json["healthy"] is True


def test_system_health_job_fails_when_db_unreachable(db_session, monkeypatch):
    monkeypatch.setattr(
        system_health_module,
        "check_database_health",
        lambda engine: HealthCheckResult(
            healthy=False,
            detail="OperationalError: database connection failed",
            checked_at=datetime.now(UTC),
        ),
    )

    job = execute_job(
        db_session,
        job_type=JobType.SYSTEM_HEALTH,
        trigger_type=TriggerType.LOCAL,
    )
    assert job.status == JobStatus.FAILED
    assert job.error_code == "DATABASE_UNREACHABLE"
