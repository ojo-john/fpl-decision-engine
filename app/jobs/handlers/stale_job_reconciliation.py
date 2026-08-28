"""STALE_JOB_RECONCILIATION job: marks jobs (and their linked run-header row) FAILED once they've
sat in RUNNING far longer than that job type could ever legitimately still be in-process.

Real incident this exists for (2026-08-27/28): querying real production data found only 3 of 7
real ``transfer_optimization_runs`` ever reached a terminal status -- the other 4 sat in
``RUNNING`` permanently. Root cause: ``app.jobs.service.execute_job`` inserts a ``RUNNING`` row
BEFORE doing any real work specifically so a crash still leaves a terminal row (via its own
``try/except Exception`` writing ``FAILED``) -- but that safety net only runs if the PROCESS
itself keeps running long enough to reach the ``except``/``finally`` block. A Render redeploy
restarting the container mid-request, an OOM kill, or resource contention from a concurrent
request on the same small free-tier instance all kill the process directly -- nothing ever runs
that code, so the row just sits ``RUNNING`` forever. Confirmed two distinct real triggers for this
exact incident: an unrelated ``git push`` mid-investigation (Render auto-deploys on every push)
killed one in-flight request, and two other stuck runs started within 2 minutes of two others that
DID succeed, consistent with concurrent requests starving each other on a single small container.

Checked every scheduler tick (mirrors ``DEADLINE_REMINDER``'s own "correctness from an internal
check, not from throttling how often this runs" precedent) rather than interval-gated -- this is
cheap (a single indexed query, typically zero matching rows) and there is no real downside to
checking often; the only thing that matters is a stale row eventually getting reconciled, and
every-tick reconciliation minimizes how long a manager-visible page keeps showing a run as
"still going" when it's actually dead.

Per-job-type thresholds, not one global number: different job types have wildly different real
legitimate runtimes (an H2H simulation is a single Monte Carlo pass; a full-season chip schedule
evaluation re-runs that Monte Carlo pass many times over). A threshold that's safe for the
lightest job type would false-positive-fail a genuinely still-running heavy one; a threshold safe
for the heaviest would leave a truly-dead lightweight job's row stale for hours. Thresholds below
are grounded in real observed durations from this project's own history, not guessed -- see each
comment.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.jobs.registry import register_handler
from app.jobs.runner import JobContext, JobResult, JobRunner
from packages.database.models.backtest_run import BacktestRun
from packages.database.models.chip_optimization_run import ChipOptimizationRun
from packages.database.models.chip_schedule_optimization_run import ChipScheduleOptimizationRun
from packages.database.models.cold_start_validation_run import ColdStartValidationRun
from packages.database.models.h2h_simulation_run import H2HSimulationRun
from packages.database.models.ingestion_run import IngestionRun
from packages.database.models.job import Job
from packages.database.models.league_sync_run import LeagueSyncRun
from packages.database.models.learning_report import LearningReport
from packages.database.models.optimization_run import OptimizationRun
from packages.database.models.position_segmentation_validation_run import (
    PositionSegmentationValidationRun,
)
from packages.database.models.transfer_optimization_run import TransferOptimizationRun
from packages.database.session import SessionLocal
from packages.domain.chips import ChipOptimizationRunStatus, ChipScheduleRunStatus
from packages.domain.h2h import H2HSimulationRunStatus, LeagueSyncRunStatus
from packages.domain.ingestion import IngestionRunStatus
from packages.domain.jobs import JobStatus, JobType
from packages.domain.learning import (
    BacktestRunStatus,
    ColdStartValidationRunStatus,
    GameweekReviewRunStatus,
    PositionSegmentationValidationRunStatus,
)
from packages.domain.optimization import OptimizationRunStatus
from packages.domain.transfers import TransferOptimizationRunStatus

ORPHANED_ERROR_CODE = "ORPHANED_PROCESS_KILLED"

_ORPHANED_ERROR_MESSAGE_TEMPLATE = (
    "Orphaned: still RUNNING more than {threshold_minutes} minutes after it started, with no "
    "terminal status ever written. app.jobs.service.execute_job's own 'insert RUNNING before "
    "work' safety net only protects against in-process exceptions -- it can't write a terminal "
    "status if the process itself was killed externally (a Render redeploy, OOM, resource "
    "contention from a concurrent request). Marked FAILED by STALE_JOB_RECONCILIATION, not a "
    "real in-process failure."
)

# Every real successful TRANSFER_OPTIMIZATION run (this app's heaviest single-request job:
# multi-scenario horizon search) has finished within 40 minutes -- 90 gives real margin.
# SQUAD/LINEUP_OPTIMIZATION do one MIP solve, not a multi-scenario search -- lighter, 45 min.
# CHIP_SCHEDULE_OPTIMIZATION's Phase 2 (full-season, path-dependent) is documented as
# "the same real-world territory as the 10-40 minute TRANSFER_OPTIMIZATION runs" but re-runs its
# own Monte Carlo evaluators many times over -- 120 min. H2H_SIMULATION is a single Monte Carlo
# pass, genuinely fast even on this project's slow free-tier infra -- 30 min is already generous.
# DATA_INGESTION/RULES_SYNC hit FPL's own live API for one season's worth of data -- should be
# minutes; HISTORICAL_DATA_INGESTION processes many seasons -- 90 min. Anything not listed here
# falls back to _DEFAULT_STALE_THRESHOLD.
_STALE_THRESHOLDS: dict[JobType, timedelta] = {
    JobType.TRANSFER_OPTIMIZATION: timedelta(minutes=90),
    JobType.SQUAD_OPTIMIZATION: timedelta(minutes=45),
    JobType.LINEUP_OPTIMIZATION: timedelta(minutes=45),
    JobType.CHIP_SIMULATION: timedelta(minutes=90),
    JobType.CHIP_SCHEDULE_OPTIMIZATION: timedelta(minutes=120),
    JobType.H2H_SIMULATION: timedelta(minutes=30),
    JobType.BACKTEST: timedelta(minutes=60),
    JobType.COLD_START_PROJECTION_VALIDATION: timedelta(minutes=60),
    JobType.POSITION_SEGMENTATION_VALIDATION: timedelta(minutes=60),
    JobType.LEAGUE_SYNC: timedelta(minutes=30),
    JobType.GAMEWEEK_REVIEW: timedelta(minutes=30),
    JobType.DATA_INGESTION: timedelta(minutes=30),
    JobType.HISTORICAL_DATA_INGESTION: timedelta(minutes=90),
    JobType.RULES_SYNC: timedelta(minutes=15),
}
# Matches train-production.yml's own 180-minute ceiling -- the single longest legitimate real
# run duration observed in this project (MODEL_TRAINING against the full "all seasons" dataset).
_DEFAULT_STALE_THRESHOLD = timedelta(minutes=180)


def stale_threshold_for(job_type: JobType) -> timedelta:
    """Pure lookup, exposed for direct unit testing without a database."""
    return _STALE_THRESHOLDS.get(job_type, _DEFAULT_STALE_THRESHOLD)


@dataclass(frozen=True)
class _RunHeaderMapping:
    """Which run-header table (if any) a job type's own row lives in, and its RUNNING/FAILED
    values -- not every job type has one (e.g. MODEL_TRAINING's provenance lives on
    ``model_versions.training_job_id``, which has no RUNNING/FAILED lifecycle of its own to
    reconcile); those are still caught at the ``jobs`` table level, just with no second row to
    finalize."""

    model: type[Any]
    status_attribute: str
    running_value: object
    failed_value: object


_RUN_HEADER_MAPPINGS: dict[JobType, _RunHeaderMapping] = {
    JobType.TRANSFER_OPTIMIZATION: _RunHeaderMapping(
        TransferOptimizationRun,
        "run_status",
        TransferOptimizationRunStatus.RUNNING,
        TransferOptimizationRunStatus.FAILED,
    ),
    JobType.SQUAD_OPTIMIZATION: _RunHeaderMapping(
        OptimizationRun, "run_status", OptimizationRunStatus.RUNNING, OptimizationRunStatus.FAILED
    ),
    JobType.LINEUP_OPTIMIZATION: _RunHeaderMapping(
        OptimizationRun, "run_status", OptimizationRunStatus.RUNNING, OptimizationRunStatus.FAILED
    ),
    JobType.CHIP_SIMULATION: _RunHeaderMapping(
        ChipOptimizationRun,
        "run_status",
        ChipOptimizationRunStatus.RUNNING,
        ChipOptimizationRunStatus.FAILED,
    ),
    JobType.CHIP_SCHEDULE_OPTIMIZATION: _RunHeaderMapping(
        ChipScheduleOptimizationRun,
        "run_status",
        ChipScheduleRunStatus.RUNNING,
        ChipScheduleRunStatus.FAILED,
    ),
    JobType.H2H_SIMULATION: _RunHeaderMapping(
        H2HSimulationRun, "run_status", H2HSimulationRunStatus.RUNNING, H2HSimulationRunStatus.FAILED
    ),
    JobType.BACKTEST: _RunHeaderMapping(
        BacktestRun, "run_status", BacktestRunStatus.RUNNING, BacktestRunStatus.FAILED
    ),
    JobType.COLD_START_PROJECTION_VALIDATION: _RunHeaderMapping(
        ColdStartValidationRun,
        "run_status",
        ColdStartValidationRunStatus.RUNNING,
        ColdStartValidationRunStatus.FAILED,
    ),
    JobType.POSITION_SEGMENTATION_VALIDATION: _RunHeaderMapping(
        PositionSegmentationValidationRun,
        "run_status",
        PositionSegmentationValidationRunStatus.RUNNING,
        PositionSegmentationValidationRunStatus.FAILED,
    ),
    JobType.LEAGUE_SYNC: _RunHeaderMapping(
        LeagueSyncRun, "run_status", LeagueSyncRunStatus.RUNNING, LeagueSyncRunStatus.FAILED
    ),
    JobType.GAMEWEEK_REVIEW: _RunHeaderMapping(
        LearningReport, "run_status", GameweekReviewRunStatus.RUNNING, GameweekReviewRunStatus.FAILED
    ),
    JobType.DATA_INGESTION: _RunHeaderMapping(
        IngestionRun, "status", IngestionRunStatus.RUNNING, IngestionRunStatus.FAILED
    ),
    JobType.HISTORICAL_DATA_INGESTION: _RunHeaderMapping(
        IngestionRun, "status", IngestionRunStatus.RUNNING, IngestionRunStatus.FAILED
    ),
    JobType.RULES_SYNC: _RunHeaderMapping(
        IngestionRun, "status", IngestionRunStatus.RUNNING, IngestionRunStatus.FAILED
    ),
}


def _fail_run_header_row(session: Session, job: Job, now: datetime, error_message: str) -> bool:
    """Finalize this job's own run-header row (if a mapping is registered for its job type) to
    FAILED, mirroring exactly what ``execute_job``'s own ``except`` block would have written had
    the process not been killed externally. Returns True if a row was found and updated -- False
    is a real, expected outcome for job types with no run-header table (see
    ``_RunHeaderMapping``'s own docstring), not a sign anything went wrong.
    """
    mapping = _RUN_HEADER_MAPPINGS.get(job.job_type)
    if mapping is None:
        return False
    run = session.execute(select(mapping.model).where(mapping.model.job_id == job.id)).scalar_one_or_none()
    if run is None or getattr(run, mapping.status_attribute) != mapping.running_value:
        return False
    setattr(run, mapping.status_attribute, mapping.failed_value)
    run.error_code = ORPHANED_ERROR_CODE
    run.error_message = error_message
    run.completed_at = now
    return True


@register_handler(JobType.STALE_JOB_RECONCILIATION)
class StaleJobReconciliationJobRunner(JobRunner):
    """Finds every ``jobs`` row still ``RUNNING`` past its job type's own staleness threshold and
    marks it (and its linked run-header row, if one exists) ``FAILED``. Always ``SUCCEEDED`` --
    finding zero stale jobs is the overwhelmingly common, correct outcome, not a failure to
    report; only a genuine DB error reaching this job's own (absent) try/except would fail it,
    matching ``DEADLINE_REMINDER``'s identical precedent.
    """

    def run(self, ctx: JobContext) -> JobResult:
        session = SessionLocal()
        try:
            now = datetime.now(UTC)
            running_jobs = session.execute(select(Job).where(Job.status == JobStatus.RUNNING)).scalars().all()

            reconciled: list[dict] = []
            for job in running_jobs:
                # Never reconcile a currently-in-progress reconciliation job against itself --
                # this job's own row is RUNNING for the exact duration of this very function call.
                if job.job_type == JobType.STALE_JOB_RECONCILIATION:
                    continue
                # started_at is always set in the same statement that sets status=RUNNING (see
                # app.jobs.service.execute_job) -- this guard is for mypy's benefit only, since
                # the column itself is nullable for QUEUED rows this query never selects.
                if job.started_at is None:
                    continue
                threshold = stale_threshold_for(job.job_type)
                if now - job.started_at <= threshold:
                    continue

                error_message = _ORPHANED_ERROR_MESSAGE_TEMPLATE.format(
                    threshold_minutes=int(threshold.total_seconds() // 60)
                )
                job.status = JobStatus.FAILED
                job.error_code = ORPHANED_ERROR_CODE
                job.error_message = error_message
                job.completed_at = now
                run_header_updated = _fail_run_header_row(session, job, now, error_message)
                reconciled.append(
                    {
                        "job_id": str(job.id),
                        "job_type": job.job_type.value,
                        "started_at": job.started_at.isoformat(),
                        "run_header_updated": run_header_updated,
                    }
                )

            session.commit()
            return JobResult(
                status=JobStatus.SUCCEEDED,
                output={"reconciled_count": len(reconciled), "reconciled": reconciled},
                error_code=None,
                error_message=None,
            )
        finally:
            session.close()
