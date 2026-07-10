"""Orchestrates the full lifecycle of a job run: create, execute, record result."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.jobs.registry import get_handler
from app.jobs.runner import JobContext
from packages.database.models.job import Job
from packages.domain.jobs import JobStatus, JobType, TriggerType


def execute_job(
    session: Session,
    job_type: JobType,
    parameters: dict[str, Any] | None = None,
    requested_by: str | None = None,
    trigger_type: TriggerType = TriggerType.LOCAL,
    idempotency_key: str | None = None,
    git_commit_sha: str | None = None,
    application_version: str | None = None,
) -> Job:
    """Execute a job, recording its lifecycle in the ``jobs`` table.

    If ``idempotency_key`` is provided and a job with that key already
    exists, that existing job is returned unchanged — no new row is
    inserted and the handler is not re-executed.
    """
    if idempotency_key is not None:
        existing = session.execute(
            select(Job).where(Job.idempotency_key == idempotency_key)
        ).scalar_one_or_none()
        if existing is not None:
            return existing

    job = Job(
        job_type=job_type,
        requested_by=requested_by,
        trigger_type=trigger_type,
        status=JobStatus.QUEUED,
        parameters_json=parameters,
        attempt_number=1,
        idempotency_key=idempotency_key,
        git_commit_sha=git_commit_sha,
        application_version=application_version,
    )
    session.add(job)
    session.commit()
    session.refresh(job)

    job.status = JobStatus.RUNNING
    job.started_at = datetime.now(UTC)
    session.commit()

    ctx = JobContext(
        job_id=str(job.id),
        job_type=job_type,
        parameters=parameters,
        attempt_number=job.attempt_number,
        requested_by=requested_by,
        trigger_type=trigger_type,
    )

    try:
        handler_cls = get_handler(job_type)
        handler = handler_cls()
        result = handler.run(ctx)
        job.status = result.status
        job.output_json = result.output
        job.error_code = result.error_code
        job.error_message = result.error_message
    except Exception as exc:  # noqa: BLE001 - job row must always reach a terminal status
        job.status = JobStatus.FAILED
        job.error_code = "UNHANDLED_EXCEPTION"
        job.error_message = str(exc)

    job.completed_at = datetime.now(UTC)
    session.commit()
    session.refresh(job)

    return job
