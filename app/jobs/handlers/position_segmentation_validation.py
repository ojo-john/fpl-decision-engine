"""POSITION_SEGMENTATION_VALIDATION job: real, walk-forward evidence for whether training a
SEPARATE model per position (GKP/DEF/MID/FWD) would out-predict the existing single global model.

Closes a gap ``app.modeling.promotion`` previously named explicitly but could not compute: "there
is no per-position actuals breakdown computed anywhere in this codebase yet." See
``app.modeling.position_segmentation`` for the real evaluation harness this handler wraps (reuses
``app.modeling.walk_forward``'s exact fold-building -- never reimplemented) and
``packages.database.models.position_segmentation_validation_run`` for why this persists to its own
dedicated table rather than touching ``model_versions``.

Deliberately read-only with respect to the live champion/promotion state: this job never fits an
artifact that gets serialized, never inserts a ``model_versions`` row, never calls
``app.modeling.promotion``'s promotion-decision functions. It answers a diagnostic question
("would position-specific models help, and for which positions specifically?"), not a "should we
promote" question -- that remains a separate, later, explicitly human-reviewed decision informed
by this job's real numbers.

"Build fully in memory, insert once" persistence discipline, mirroring ``BACKTEST``
(``app.jobs.handlers.backtest``) exactly -- this is pure computation over already-persisted,
read-only ``player_features`` data with no other side-effecting steps to sequence, so there is no
benefit to a create-early-RUNNING-row discipline: on any failure, nothing has been written yet.

``ctx.parameters`` accepts ``{"feature_version": 1}`` (defaults to
``app.features.service.FEATURE_VERSION`` if omitted) -- mirrors ``MODEL_TRAINING``'s own default.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.features.service import FEATURE_VERSION
from app.jobs.registry import register_handler
from app.jobs.runner import JobContext, JobResult, JobRunner
from app.modeling.position_segmentation import PositionSegmentationResult, evaluate_position_segmentation
from app.modeling.walk_forward import build_expanding_window_folds, usable_season_labels_with_features
from packages.database.models.player_features import PlayerFeatures
from packages.database.models.position_segmentation_validation_run import PositionSegmentationValidationRun
from packages.database.models.season import Season
from packages.database.session import SessionLocal
from packages.domain.jobs import JobStatus, JobType
from packages.domain.learning import PositionSegmentationValidationRunStatus


def _to_decimal(value: float | None) -> Decimal | None:
    """Round to 4 decimal places -- mirrors every other Numeric(*, 4) column in this codebase's own
    ``Decimal(str(round(value, 4)))`` convention (e.g. ``app.jobs.handlers.backtest``).
    """
    return Decimal(str(round(value, 4))) if value is not None else None


@register_handler(JobType.POSITION_SEGMENTATION_VALIDATION)
class PositionSegmentationValidationJobRunner(JobRunner):
    """Runs POSITION_SEGMENTATION_VALIDATION for one ``feature_version``: real walk-forward,
    per-position MAE comparison between the existing global-model architecture and a
    position-specific alternative. See module docstring for the full scope.
    """

    def run(self, ctx: JobContext) -> JobResult:
        session = SessionLocal()
        try:
            feature_version = (ctx.parameters or {}).get("feature_version", FEATURE_VERSION)

            usable_seasons = usable_season_labels_with_features(session, feature_version)
            if not usable_seasons:
                return JobResult(
                    status=JobStatus.FAILED,
                    output=None,
                    error_code="NO_SEASONS_WITH_FEATURES",
                    error_message=(
                        f"cannot evaluate position segmentation -- no season has any player_features "
                        f"rows for feature_version={feature_version} (run FEATURE_GENERATION first)"
                    ),
                )

            folds = build_expanding_window_folds(usable_seasons)
            rows_by_season = self._load_feature_rows_by_season(session, usable_seasons, feature_version)

            started_at = datetime.now(UTC)
            result = evaluate_position_segmentation(rows_by_season, folds, usable_seasons)
            completed_at = datetime.now(UTC)

            if result.n_observations == 0:
                return JobResult(
                    status=JobStatus.FAILED,
                    output=None,
                    error_code="NO_POSITION_SCORED",
                    error_message=(
                        "no (fold, position) pair produced a single successfully-scored observation "
                        "for this feature_version against the current dataset."
                    ),
                )

            row = PositionSegmentationValidationRun(
                job_id=uuid.UUID(ctx.job_id),
                feature_version=feature_version,
                run_status=PositionSegmentationValidationRunStatus.SUCCEEDED,
                usable_seasons_json=result.usable_seasons,
                fold_count=result.fold_count,
                degenerate_single_season=result.degenerate_single_season,
                per_position_json=self._per_position_json(result),
                overall_global_mae=_to_decimal(result.overall_global_mae),
                overall_position_specific_mae=_to_decimal(result.overall_position_specific_mae),
                n_observations=result.n_observations,
                started_at=started_at,
                completed_at=completed_at,
            )
            session.add(row)
            session.commit()
            session.refresh(row)

            return JobResult(
                status=JobStatus.SUCCEEDED,
                output={
                    "position_segmentation_validation_run_id": str(row.id),
                    "feature_version": feature_version,
                    "usable_seasons": result.usable_seasons,
                    "fold_count": result.fold_count,
                    "degenerate_single_season": result.degenerate_single_season,
                    "per_position": self._per_position_json(result),
                    "overall_global_mae": result.overall_global_mae,
                    "overall_position_specific_mae": result.overall_position_specific_mae,
                    "n_observations": result.n_observations,
                },
                error_code=None,
                error_message=None,
            )
        except Exception as exc:  # noqa: BLE001 - job row must always reach a terminal status
            session.rollback()
            return JobResult(
                status=JobStatus.FAILED,
                output=None,
                error_code="POSITION_SEGMENTATION_VALIDATION_FAILED",
                error_message=str(exc),
            )
        finally:
            session.close()

    @staticmethod
    def _per_position_json(result: PositionSegmentationResult) -> dict[str, Any]:
        """JSON-safe (plain float, not Decimal) per-position breakdown -- matches
        ``PositionSegmentationValidationRun.per_position_json``'s own JSONB column shape.
        """
        return {
            position: {
                "global_mae": values["global_mae"],
                "position_specific_mae": values["position_specific_mae"],
                "mae_gap_pct": values["mae_gap_pct"],
                "n_observations": values["n_observations"],
            }
            for position, values in result.per_position.items()
        }

    def _load_feature_rows_by_season(
        self, session: Session, season_labels: list[str], feature_version: int
    ) -> dict[str, list[PlayerFeatures]]:
        """Mirrors ``app.jobs.handlers.model_training``'s own identically-shaped preload."""
        season_id_by_label: dict[str, uuid.UUID] = {
            row.label: row.id
            for row in session.execute(select(Season.label, Season.id).where(Season.label.in_(season_labels)))
        }
        rows_by_season: dict[str, list[PlayerFeatures]] = {}
        for label in season_labels:
            season_id = season_id_by_label[label]
            rows_by_season[label] = list(
                session.execute(
                    select(PlayerFeatures).where(
                        PlayerFeatures.season_id == season_id,
                        PlayerFeatures.feature_version == feature_version,
                    )
                )
                .scalars()
                .all()
            )
        return rows_by_season
