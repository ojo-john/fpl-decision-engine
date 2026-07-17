"""DATA_INGESTION job: the full ingestion pass (rules sync + teams/positions/players/gameweeks/fixtures).

Orchestrated via ``app.ingestion.service.run_ingestion``, which internally
reuses ``app.rules.service.sync_rules`` as one step (no second bootstrap-static
fetch), and also backfills live per-gameweek actual stats
(``player_gameweek_stats``, ``data_source="live_event"``) for every
newly-finished gameweek — see ``app.ingestion.live_stats_normalization``
(Milestone 7 Layer 0). That table was previously populated only by
historical CSV ingestion; this closes the gap for the current, in-progress
season.
"""

import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.ingestion.service import BootstrapFetchBlockedError, run_ingestion
from app.jobs.registry import register_handler
from app.jobs.runner import JobContext, JobResult, JobRunner
from packages.database.models.ingestion_run import IngestionRun
from packages.database.session import SessionLocal
from packages.domain.ingestion import IngestionRunStatus, IngestionRunType
from packages.domain.jobs import JobStatus, JobType
from packages.fpl_client import FPLClient

REPORTS_DIR = Path(__file__).resolve().parents[3] / "reports" / "ingestion"


def _write_local_report(ingestion_run_id: uuid.UUID, report_markdown: str) -> None:
    """Best-effort local convenience copy for `make ingest` runs — never fatal if it fails."""
    try:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        (REPORTS_DIR / f"{ingestion_run_id}.md").write_text(report_markdown)
    except OSError:
        pass


@register_handler(JobType.DATA_INGESTION)
class DataIngestionJobRunner(JobRunner):
    """Runs a full ingestion pass and returns a JobResult carrying the ingestion report.

    Wraps its entire orchestration in its own try/except so the
    ``ingestion_runs`` row always reaches a terminal status — the outer
    ``execute_job`` try/except only guarantees the ``jobs`` row does.
    """

    def run(self, ctx: JobContext) -> JobResult:
        session = SessionLocal()
        run_row: IngestionRun | None = None
        try:
            started_at = datetime.now(UTC)
            run_row = IngestionRun(
                job_id=uuid.UUID(ctx.job_id),
                run_type=IngestionRunType.DATA_INGESTION,
                status=IngestionRunStatus.RUNNING,
                started_at=started_at,
            )
            session.add(run_row)
            session.commit()
            session.refresh(run_row)

            client = FPLClient()
            try:
                result = run_ingestion(session, run_row.id, client, started_at)
            finally:
                client.close()

            session.commit()

            _write_local_report(run_row.id, result["report_markdown"])

            job_status = (
                JobStatus.SUCCEEDED
                if result["status"]
                in (IngestionRunStatus.SUCCEEDED.value, IngestionRunStatus.SUCCEEDED_WITH_WARNINGS.value)
                else JobStatus.FAILED
            )

            return JobResult(
                status=job_status,
                output=result,
                error_code=None,
                error_message=None,
            )
        except BootstrapFetchBlockedError as exc:
            session.rollback()
            self._finalize_failed(session, run_row)
            return JobResult(
                status=JobStatus.FAILED,
                output={"ingestion_run_id": str(run_row.id)} if run_row is not None else None,
                error_code="BOOTSTRAP_FETCH_BLOCKED",
                error_message=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - ingestion_runs row must always reach a terminal status
            session.rollback()
            self._finalize_failed(session, run_row)
            return JobResult(
                status=JobStatus.FAILED,
                output={"ingestion_run_id": str(run_row.id)} if run_row is not None else None,
                error_code="DATA_INGESTION_FAILED",
                error_message=str(exc),
            )
        finally:
            session.close()

    def _finalize_failed(self, session, run_row: IngestionRun | None) -> None:
        if run_row is None or run_row.id is None:
            return
        try:
            fresh = session.get(IngestionRun, run_row.id)
            if fresh is not None and fresh.status == IngestionRunStatus.RUNNING:
                fresh.status = IngestionRunStatus.FAILED
                fresh.completed_at = datetime.now(UTC)
                session.commit()
        except Exception:  # noqa: BLE001 - best-effort finalization only
            session.rollback()
