"""RULES_SYNC job: a lighter-weight, manual-only job for on-demand rule-change checks.

Fetches bootstrap-static once and calls the same ``app.rules.service.sync_rules``
function that ``DATA_INGESTION`` reuses internally — the two never fetch
bootstrap-static independently or duplicate extraction/diff logic.
"""

import uuid
from datetime import UTC, datetime

from app.ingestion.service import get_or_create_current_season, persist_raw_snapshot
from app.jobs.registry import register_handler
from app.jobs.runner import JobContext, JobResult, JobRunner
from app.rules.service import sync_rules
from packages.database.models.ingestion_run import IngestionRun
from packages.database.session import SessionLocal
from packages.domain.ingestion import IngestionRunStatus, IngestionRunType
from packages.domain.jobs import JobStatus, JobType
from packages.fpl_client import FPLClient


@register_handler(JobType.RULES_SYNC)
class RulesSyncJobRunner(JobRunner):
    """Fetches bootstrap-static and syncs the FPL rules/scoring configuration.

    Each run gets its own ``ingestion_runs`` row (``run_type=rules_sync``).
    Any exception during orchestration is caught here so the
    ``ingestion_runs`` row always reaches a terminal status, even though the
    outer ``execute_job`` try/except only guarantees the ``jobs`` row does.
    """

    def run(self, ctx: JobContext) -> JobResult:
        session = SessionLocal()
        run_row: IngestionRun | None = None
        try:
            started_at = datetime.now(UTC)
            run_row = IngestionRun(
                job_id=uuid.UUID(ctx.job_id),
                run_type=IngestionRunType.RULES_SYNC,
                status=IngestionRunStatus.RUNNING,
                started_at=started_at,
            )
            session.add(run_row)
            session.commit()
            session.refresh(run_row)

            client = FPLClient()
            try:
                fetch_result = client.fetch_bootstrap_static()
            finally:
                client.close()

            persist_raw_snapshot(session, run_row.id, fetch_result)
            session.commit()

            if fetch_result.raw_body is None:
                run_row.status = IngestionRunStatus.FAILED
                run_row.overall_health = fetch_result.health_status
                run_row.completed_at = datetime.now(UTC)
                session.commit()
                return JobResult(
                    status=JobStatus.FAILED,
                    output={"ingestion_run_id": str(run_row.id)},
                    error_code="BOOTSTRAP_FETCH_BLOCKED",
                    error_message=fetch_result.error_message
                    or "bootstrap-static fetch failed with no usable body",
                )

            bootstrap_json = fetch_result.raw_body
            assert isinstance(bootstrap_json, dict)  # bootstrap-static always returns a JSON object
            season = get_or_create_current_season(session, bootstrap_json)

            result = sync_rules(
                session,
                bootstrap_json=bootstrap_json,
                fetched_at=fetch_result.retrieved_at,
                ingestion_run_id=run_row.id,
                season_id=season.id,
                endpoint_health=fetch_result.health_status,
            )

            run_row.status = IngestionRunStatus.SUCCEEDED
            run_row.overall_health = fetch_result.health_status
            run_row.completed_at = datetime.now(UTC)
            run_row.summary_json = result
            session.commit()

            return JobResult(
                status=JobStatus.SUCCEEDED,
                output={"ingestion_run_id": str(run_row.id), **result},
                error_code=None,
                error_message=None,
            )
        except Exception as exc:  # noqa: BLE001 - ingestion_runs row must always reach a terminal status
            session.rollback()
            if run_row is not None and run_row.id is not None:
                try:
                    run_row = session.get(IngestionRun, run_row.id)
                    if run_row is not None and run_row.status == IngestionRunStatus.RUNNING:
                        run_row.status = IngestionRunStatus.FAILED
                        run_row.completed_at = datetime.now(UTC)
                        session.commit()
                except Exception:  # noqa: BLE001 - best-effort finalization only
                    session.rollback()
            return JobResult(
                status=JobStatus.FAILED,
                output={"ingestion_run_id": str(run_row.id)} if run_row is not None else None,
                error_code="RULES_SYNC_FAILED",
                error_message=str(exc),
            )
        finally:
            session.close()
