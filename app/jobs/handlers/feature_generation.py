"""FEATURE_GENERATION job: computes leakage-safe player_features rows for one (or every) season.

Orchestrated via ``app.features.service.generate_features_for_season``, one
season at a time. No dedicated run-header table is needed here (unlike
``ingestion_runs`` for the ingestion jobs) — each ``player_features`` row
already carries its own ``computed_at``/``feature_version``, so
``jobs.output_json`` (row counts, seasons covered, feature_version) is
sufficient auditability, per the Layer 1 plan's own design note.

``ctx.parameters`` must contain ``{"season_label": "2023-24"}`` for one
season, or ``{"season_label": "all"}`` to generate features for every season
that has at least one ``player_gameweek_stats`` row — determined at runtime
via a DB query, never hardcoded (mirrors HISTORICAL_DATA_INGESTION's own
"all" convention, but scoped to whatever seasons already have ingested stats
rather than a fixed earliest-season constant).
"""

from dataclasses import asdict
from typing import Any

from sqlalchemy import select

from app.features.service import FEATURE_VERSION, generate_features_for_season
from app.jobs.registry import register_handler
from app.jobs.runner import JobContext, JobResult, JobRunner
from packages.database.models.player_gameweek_stats import PlayerGameweekStats
from packages.database.models.season import Season
from packages.database.session import SessionLocal
from packages.domain.jobs import JobStatus, JobType


@register_handler(JobType.FEATURE_GENERATION)
class FeatureGenerationJobRunner(JobRunner):
    """Runs FEATURE_GENERATION for one season, or every season with ingested stats ("all")."""

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

            feature_version = (ctx.parameters or {}).get("feature_version", FEATURE_VERSION)

            if season_label_param == "all":
                season_rows = session.execute(
                    select(Season.id, Season.label)
                    .join(PlayerGameweekStats, PlayerGameweekStats.season_id == Season.id)
                    .distinct()
                ).all()
                if not season_rows:
                    return JobResult(
                        status=JobStatus.FAILED,
                        output=None,
                        error_code="NO_SEASONS_WITH_STATS",
                        error_message=(
                            "cannot generate features for 'all' — no season has any player_gameweek_stats "
                            "rows yet (run DATA_INGESTION/HISTORICAL_DATA_INGESTION first)"
                        ),
                    )
                seasons = [(row.id, row.label) for row in season_rows]
            else:
                season_row = session.execute(
                    select(Season.id, Season.label).where(Season.label == season_label_param)
                ).one_or_none()
                if season_row is None:
                    return JobResult(
                        status=JobStatus.FAILED,
                        output=None,
                        error_code="UNKNOWN_SEASON_LABEL",
                        error_message=f"no season found with label {season_label_param!r}",
                    )
                seasons = [(season_row.id, season_row.label)]

            season_results: dict[str, Any] = {}
            for season_id, season_label in seasons:
                counts = generate_features_for_season(session, season_id, feature_version=feature_version)
                session.commit()
                season_results[season_label] = asdict(counts)

            return JobResult(
                status=JobStatus.SUCCEEDED,
                output={
                    "season_labels": [label for _, label in seasons],
                    "feature_version": feature_version,
                    "season_results": season_results,
                },
                error_code=None,
                error_message=None,
            )
        except Exception as exc:  # noqa: BLE001 - job row must always reach a terminal status
            session.rollback()
            return JobResult(
                status=JobStatus.FAILED,
                output=None,
                error_code="FEATURE_GENERATION_FAILED",
                error_message=str(exc),
            )
        finally:
            session.close()
