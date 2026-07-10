"""Integration tests proving idempotency is enforced both at the app layer
(app.jobs.service.execute_job) and at the database layer (unique index on
jobs.idempotency_key).

Requires a running Postgres with migrations applied (see conftest.py).
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.jobs.service import execute_job
from packages.database.models.job import Job
from packages.domain.jobs import JobStatus, JobType, TriggerType


def test_execute_job_is_idempotent(db_session):
    key = f"test-idempotency-{uuid.uuid4()}"

    first = execute_job(
        db_session,
        job_type=JobType.SYSTEM_HEALTH,
        trigger_type=TriggerType.LOCAL,
        idempotency_key=key,
    )
    second = execute_job(
        db_session,
        job_type=JobType.SYSTEM_HEALTH,
        trigger_type=TriggerType.LOCAL,
        idempotency_key=key,
    )

    assert first.id == second.id

    count = db_session.execute(select(Job).where(Job.idempotency_key == key)).scalars().all()
    assert len(count) == 1


def test_duplicate_idempotency_key_violates_db_constraint(db_session):
    key = f"test-idempotency-direct-{uuid.uuid4()}"

    job1 = Job(
        job_type=JobType.SYSTEM_HEALTH,
        trigger_type=TriggerType.LOCAL,
        status=JobStatus.QUEUED,
        idempotency_key=key,
    )
    db_session.add(job1)
    db_session.commit()

    job2 = Job(
        job_type=JobType.SYSTEM_HEALTH,
        trigger_type=TriggerType.LOCAL,
        status=JobStatus.QUEUED,
        idempotency_key=key,
    )
    db_session.add(job2)
    with pytest.raises(IntegrityError):
        db_session.commit()

    db_session.rollback()
