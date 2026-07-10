"""Tests for app.jobs.registry: handler lookup for JobType values."""

import pytest

from app.jobs.handlers.system_health import SystemHealthJobRunner
from app.jobs.registry import JobHandlerNotImplementedError, get_handler
from packages.domain.jobs import JobType


def test_system_health_resolves_to_its_runner():
    assert get_handler(JobType.SYSTEM_HEALTH) is SystemHealthJobRunner


@pytest.mark.parametrize(
    "job_type",
    [jt for jt in JobType if jt is not JobType.SYSTEM_HEALTH],
)
def test_unimplemented_job_types_raise(job_type):
    with pytest.raises(JobHandlerNotImplementedError):
        get_handler(job_type)
