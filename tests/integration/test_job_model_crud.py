"""Integration test: basic CRUD against the real `jobs` table.

Requires a running Postgres with migrations applied (see conftest.py).
"""

import uuid

from sqlalchemy import select

from packages.database.models.job import Job
from packages.domain.jobs import JobStatus, JobType, TriggerType


def test_job_crud_roundtrip(db_session):
    job = Job(
        job_type=JobType.SYSTEM_HEALTH,
        trigger_type=TriggerType.LOCAL,
        status=JobStatus.QUEUED,
        parameters_json={"foo": "bar", "nested": {"a": 1}},
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    assert isinstance(job.id, uuid.UUID)

    fetched = db_session.execute(select(Job).where(Job.id == job.id)).scalar_one()
    assert fetched.parameters_json == {"foo": "bar", "nested": {"a": 1}}
    assert fetched.status == JobStatus.QUEUED

    fetched.status = JobStatus.SUCCEEDED
    fetched.output_json = {"healthy": True}
    db_session.commit()

    reloaded = db_session.execute(select(Job).where(Job.id == job.id)).scalar_one()
    assert reloaded.status == JobStatus.SUCCEEDED
    assert reloaded.output_json == {"healthy": True}

    db_session.delete(reloaded)
    db_session.commit()
