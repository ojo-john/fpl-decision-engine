"""HISTORICAL_DATA_INGESTION job: backfills one (or every usable) past season's data.

Orchestrated via ``app.ingestion.historical_service.run_historical_ingestion``,
one season at a time. Unlike ``DATA_INGESTION`` (one ``ingestion_runs`` row
per job invocation), **each season gets its own ``ingestion_runs`` row and
its own try/except** — so a failure partway through a ``"all"`` backfill
never loses, or blocks the terminal-status guarantee for, already-completed
earlier seasons.

``ctx.parameters`` must contain ``{"season_label": "2023-24"}`` for one
season, or ``{"season_label": "all"}`` to backfill every usable season from
``HistoricalFPLClient.EARLIEST_USABLE_SEASON`` up to (but excluding)
whichever season is currently live — determined at runtime via a DB query
against the live ``seasons`` table, never hardcoded.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.ingestion.historical_client import EARLIEST_USABLE_SEASON, HistoricalFPLClient
from app.ingestion.historical_service import HistoricalFetchBlockedError, run_historical_ingestion
from app.jobs.registry import register_handler
from app.jobs.runner import JobContext, JobResult, JobRunner
from packages.database.models.ingestion_run import IngestionRun
from packages.database.models.season import Season
from packages.database.session import SessionLocal
from packages.domain.ingestion import IngestionRunStatus, IngestionRunType
from packages.domain.jobs import JobStatus, JobType


def _season_start_year(season_label: str) -> int:
    return int(season_label[:4])


def _season_label_for_start_year(year: int) -> str:
    return f"{year}-{str(year + 1)[-2:]}"


def historical_season_labels_up_to(current_season_label: str) -> list[str]:
    """Every usable historical season strictly before ``current_season_label``.

    Runs from ``EARLIEST_USABLE_SEASON`` (2020-21 — the earliest season with
    a modern, ingestible schema; see ``historical_client.py``) up to, but
    never including, whichever season is currently live.
    """
    start_year = _season_start_year(EARLIEST_USABLE_SEASON)
    end_year_exclusive = _season_start_year(current_season_label)
    return [_season_label_for_start_year(year) for year in range(start_year, end_year_exclusive)]


@register_handler(JobType.HISTORICAL_DATA_INGESTION)
class HistoricalDataIngestionJobRunner(JobRunner):
    """Runs HISTORICAL_DATA_INGESTION for one season, or every usable season ("all")."""

    def run(self, ctx: JobContext) -> JobResult:
        session = SessionLocal()
        try:
            season_label_param = (ctx.parameters or {}).get("season_label")
            if not season_label_param:
                return JobResult(
                    status=JobStatus.FAILED,
                    output=None,
                    error_code="MISSING_SEASON_LABEL",
                    error_message="parameters.season_label is required (e.g. '2023-24', or 'all')",
                )

            if season_label_param == "all":
                current_season_label = session.execute(
                    select(Season.label).where(Season.is_current.is_(True))
                ).scalar_one_or_none()
                if current_season_label is None:
                    return JobResult(
                        status=JobStatus.FAILED,
                        output=None,
                        error_code="NO_CURRENT_SEASON",
                        error_message=(
                            "cannot backfill 'all' — no season is currently marked is_current in the "
                            "live seasons table (run DATA_INGESTION at least once first)"
                        ),
                    )
                season_labels = historical_season_labels_up_to(current_season_label)
            else:
                season_labels = [season_label_param]

            client = HistoricalFPLClient()
            season_results: dict[str, Any] = {}
            try:
                for season_label in season_labels:
                    season_results[season_label] = self._ingest_one_season(session, ctx, client, season_label)
            finally:
                client.close()

            any_failed = any(
                result["status"] == IngestionRunStatus.FAILED.value for result in season_results.values()
            )
            return JobResult(
                status=JobStatus.FAILED if any_failed else JobStatus.SUCCEEDED,
                output={"season_labels": season_labels, "season_results": season_results},
                error_code="ONE_OR_MORE_SEASONS_FAILED" if any_failed else None,
                error_message=None,
            )
        finally:
            session.close()

    def _ingest_one_season(
        self, session, ctx: JobContext, client: HistoricalFPLClient, season_label: str
    ) -> dict[str, Any]:
        """Runs one season end-to-end with its own ``ingestion_runs`` row.

        Wraps its entire orchestration in its own try/except so that row
        always reaches a terminal status even on an unhandled exception —
        applied per-season here since one job invocation can span many
        seasons (mirrors ``DataIngestionJobRunner``'s discipline).
        """
        run_row: IngestionRun | None = None
        try:
            started_at = datetime.now(UTC)
            run_row = IngestionRun(
                job_id=uuid.UUID(ctx.job_id),
                run_type=IngestionRunType.HISTORICAL_DATA_INGESTION,
                status=IngestionRunStatus.RUNNING,
                started_at=started_at,
            )
            session.add(run_row)
            session.commit()
            session.refresh(run_row)

            result = run_historical_ingestion(session, run_row.id, client, season_label, started_at)
            session.commit()
            return result
        except HistoricalFetchBlockedError as exc:
            session.rollback()
            self._finalize_failed(session, run_row)
            return {
                "season_label": season_label,
                "status": IngestionRunStatus.FAILED.value,
                "error_code": "HISTORICAL_FETCH_BLOCKED",
                "error_message": str(exc),
            }
        except Exception as exc:  # noqa: BLE001 - this season's ingestion_runs row must always reach a terminal status
            session.rollback()
            self._finalize_failed(session, run_row)
            return {
                "season_label": season_label,
                "status": IngestionRunStatus.FAILED.value,
                "error_code": "HISTORICAL_DATA_INGESTION_FAILED",
                "error_message": str(exc),
            }

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
