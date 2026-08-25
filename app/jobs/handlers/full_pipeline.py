"""FULL_PIPELINE job: DATA_INGESTION -> FEATURE_GENERATION -> MODEL_TRAINING in one call.

A convenience orchestrator, not a new capability: every step here already
runs correctly on its own (via `make ingest`/`make features`/`make train`,
or the `.github/workflows/` equivalents) -- this just chains the three in
sequence via `app.jobs.service.execute_job`, mirroring
`app.scheduling.tick`'s own established precedent for one job type
triggering another (`_run_job_safely` -> `execute_job`, dispatched for
`RECOMMENDATION_GENERATION`/`GAMEWEEK_REVIEW`/`LEAGUE_SYNC`/`DATA_INGESTION`/
`DEADLINE_REMINDER`/`PLAYER_ALERT_CHECK`): each nested `execute_job` call
runs on its OWN dedicated `SessionLocal()`, never this handler's session
(this handler doesn't even open one of its own -- there is no dedicated
run-header table for FULL_PIPELINE, same "no dedicated run-header table"
precedent FEATURE_GENERATION's own module docstring already established;
`jobs.output_json` recording each step's own job id/status is sufficient
auditability here too).

Step 2 (FEATURE_GENERATION) targets ONLY the season DATA_INGESTION just
touched -- not `{"season_label": "all"}` -- deliberately: MODEL_TRAINING
(step 3) auto-discovers every season that already has features from ANY
prior run (`app.modeling.walk_forward.usable_season_labels_with_features`),
so re-processing every historical season on every routine FULL_PIPELINE
call would be pure waste (each one already has its own up-to-date features
from whenever it was ingested); only the live current season's features can
possibly be stale after a fresh DATA_INGESTION run.

RECOMMENDATION_GENERATION is deliberately NOT part of this chain: it never
runs/triggers the optimizer jobs itself (see its own module docstring) --
it only correlates each of the 5 recommendation-producing jobs' already-
persisted most recent runs for a target gameweek. Since this pipeline never
produces any of those (SQUAD_OPTIMIZATION/LINEUP_OPTIMIZATION/
TRANSFER_OPTIMIZATION/H2H_SIMULATION/CHIP_SIMULATION), appending it here
would just correlate whatever (if anything) already happens to exist --
not a meaningful step of "the full pipeline" as this job type is actually
about (data/model freshness, not recommendation generation).

Fails fast at the first failing step -- a later step is never attempted
against upstream data that failed to refresh (e.g. training against stale
features because ingestion failed silently). `output["steps"]` always
records every step attempted so far, including on failure, so a caller can
tell exactly how far the pipeline got.
"""

from typing import Any

from app.jobs.registry import register_handler
from app.jobs.runner import JobContext, JobResult, JobRunner
from app.jobs.service import execute_job
from packages.database.session import SessionLocal
from packages.domain.jobs import JobStatus, JobType, TriggerType


@register_handler(JobType.FULL_PIPELINE)
class FullPipelineJobRunner(JobRunner):
    """Runs DATA_INGESTION, then FEATURE_GENERATION for that season, then MODEL_TRAINING."""

    def run(self, ctx: JobContext) -> JobResult:
        steps: dict[str, Any] = {}
        try:
            ingestion_job = self._run_nested(
                job_type=JobType.DATA_INGESTION,
                parameters=None,
            )
            steps["data_ingestion"] = {
                "job_id": str(ingestion_job.id),
                "status": ingestion_job.status.value,
            }
            if ingestion_job.status != JobStatus.SUCCEEDED:
                return JobResult(
                    status=JobStatus.FAILED,
                    output={"steps": steps},
                    error_code="DATA_INGESTION_STEP_FAILED",
                    error_message=ingestion_job.error_message,
                )
            assert ingestion_job.output_json is not None  # a SUCCEEDED run always has output_json
            season_label = ingestion_job.output_json["season_label"]
            steps["data_ingestion"]["season_label"] = season_label

            feature_job = self._run_nested(
                job_type=JobType.FEATURE_GENERATION,
                parameters={"season_label": season_label},
            )
            steps["feature_generation"] = {
                "job_id": str(feature_job.id),
                "status": feature_job.status.value,
            }
            if feature_job.status != JobStatus.SUCCEEDED:
                return JobResult(
                    status=JobStatus.FAILED,
                    output={"steps": steps},
                    error_code="FEATURE_GENERATION_STEP_FAILED",
                    error_message=feature_job.error_message,
                )

            training_job = self._run_nested(
                job_type=JobType.MODEL_TRAINING,
                parameters=None,
            )
            steps["model_training"] = {
                "job_id": str(training_job.id),
                "status": training_job.status.value,
            }
            if training_job.status != JobStatus.SUCCEEDED:
                return JobResult(
                    status=JobStatus.FAILED,
                    output={"steps": steps},
                    error_code="MODEL_TRAINING_STEP_FAILED",
                    error_message=training_job.error_message,
                )

            return JobResult(
                status=JobStatus.SUCCEEDED,
                output={"steps": steps, "season_label": season_label},
                error_code=None,
                error_message=None,
            )
        except Exception as exc:  # noqa: BLE001 - every step already reaches its own terminal
            # status via execute_job; this only guards this handler's own glue code (e.g. an
            # unexpectedly-missing output_json key) so the pipeline still reports a clear
            # error and whatever steps it got through, rather than an opaque traceback.
            return JobResult(
                status=JobStatus.FAILED,
                output={"steps": steps},
                error_code="FULL_PIPELINE_FAILED",
                error_message=str(exc),
            )

    @staticmethod
    def _run_nested(*, job_type: JobType, parameters: dict[str, Any] | None):
        """Runs one nested job on its own dedicated session -- see this module's own docstring
        for why (mirrors GameweekReviewJobRunner's established precedent)."""
        session = SessionLocal()
        try:
            return execute_job(
                session,
                job_type=job_type,
                parameters=parameters,
                requested_by="full_pipeline",
                trigger_type=TriggerType.LOCAL,
            )
        finally:
            session.close()
