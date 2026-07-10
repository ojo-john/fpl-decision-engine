"""Base types for job handlers: JobContext, JobResult, and the JobRunner ABC."""

import abc
from dataclasses import dataclass
from typing import Any

from packages.domain.jobs import JobStatus, JobType, TriggerType


@dataclass
class JobContext:
    job_id: str
    job_type: JobType
    parameters: dict[str, Any] | None
    attempt_number: int
    requested_by: str | None
    trigger_type: TriggerType


@dataclass
class JobResult:
    status: JobStatus
    output: dict[str, Any] | None
    error_code: str | None
    error_message: str | None


class JobRunner(abc.ABC):
    """Base class every job handler implements."""

    @abc.abstractmethod
    def run(self, ctx: JobContext) -> JobResult:
        raise NotImplementedError
