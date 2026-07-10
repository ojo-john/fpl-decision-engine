"""Registry mapping JobType -> JobRunner implementation.

Only ``JobType.SYSTEM_HEALTH`` is registered as of Milestone 0. Every other
``JobType`` is a real, closed enum member but has no handler implemented yet
— ``get_handler`` raises ``JobHandlerNotImplementedError`` for those. Since
``JobType`` is a closed ``str, Enum``, there is no separate "invalid value"
path to handle here: an actually-invalid value simply cannot be constructed
as a ``JobType`` in the first place (that fails at the type/enum level
before it would ever reach this registry).
"""

from app.jobs.runner import JobRunner
from packages.domain.jobs import JobType

JOB_HANDLERS: dict[JobType, type[JobRunner]] = {}


class JobHandlerNotImplementedError(Exception):
    """Raised when a JobType has no registered handler yet."""


def register_handler(job_type: JobType):
    """Class decorator that registers a JobRunner subclass for a given JobType."""

    def decorator(cls: type[JobRunner]) -> type[JobRunner]:
        JOB_HANDLERS[job_type] = cls
        return cls

    return decorator


def get_handler(job_type: JobType) -> type[JobRunner]:
    """Look up the registered handler class for a JobType.

    Raises ``JobHandlerNotImplementedError`` if no handler has been
    registered for this job type yet (true today for every JobType except
    ``SYSTEM_HEALTH``).
    """
    try:
        return JOB_HANDLERS[job_type]
    except KeyError:
        raise JobHandlerNotImplementedError(
            f"No job handler registered for job type '{job_type.value}' yet — planned for a later milestone."
        ) from None
