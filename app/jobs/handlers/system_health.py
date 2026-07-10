"""SYSTEM_HEALTH job: verifies database connectivity."""

from app.jobs.registry import register_handler
from app.jobs.runner import JobContext, JobResult, JobRunner
from packages.database.health import check_database_health
from packages.database.session import engine
from packages.domain.jobs import JobStatus, JobType


@register_handler(JobType.SYSTEM_HEALTH)
class SystemHealthJobRunner(JobRunner):
    """Runs a database connectivity check and reports the result."""

    def run(self, ctx: JobContext) -> JobResult:
        result = check_database_health(engine)
        if result.healthy:
            return JobResult(
                status=JobStatus.SUCCEEDED,
                output={"healthy": result.healthy, "detail": result.detail},
                error_code=None,
                error_message=None,
            )
        return JobResult(
            status=JobStatus.FAILED,
            output={"healthy": result.healthy, "detail": result.detail},
            error_code="DATABASE_UNREACHABLE",
            error_message=result.detail,
        )
