"""BACKTEST job: Milestone 7 Layer 6 -- walk-forward prediction-accuracy backtesting of one
ALREADY-TRAINED ``model_versions`` row, without refitting it.

Deliberately scoped narrowly, per the plan's own explicit scope cut: this is
NOT a historical decision-replay engine (it never re-runs the optimizer, chip
simulator, H2H simulator, or any recommendation-generation logic against past
gameweeks) -- it answers one focused question: "how would this SPECIFIC,
already-trained model's own walk-forward MAE look if evaluated fresh today,
against whichever historical seasons are now usable (which may have GROWN
since the model was originally trained, e.g. more seasons backfilled since
then)?" That is a genuinely different question from ``MODEL_TRAINING``'s own
walk-forward validation, which always fits 7 FRESH candidates per fold.

Mechanically, this reuses ``app.modeling.walk_forward``'s exact fold-building
(``usable_season_labels_with_features``/``build_expanding_window_folds``,
including its own single-season graceful degradation) and
``app.modeling.metrics``'/``app.modeling.champion``'s exact scoring/
aggregation functions ``app.jobs.handlers.model_training`` already uses for
its own per-fold scoring -- the only real difference is WHAT gets scored per
fold: instead of fitting a fresh candidate on ``fold.train_season_labels``
and scoring it against ``fold.validation_season_label``, this job re-uses
``model_version_id``'s own ALREADY-FITTED artifact (or, for a baseline
family, the same pure lookup/formula) UNCHANGED and scores it against every
fold's validation season -- ``fold.train_season_labels`` is read from each
``Fold`` but never used to fit anything.

Persistence: a dedicated ``backtest_runs`` table (see
``packages/database/models/backtest_run.py`` for why this is NOT layered onto
``model_metrics`` -- that table's own uniqueness constraint is scoped to one
``MODEL_TRAINING`` run's own audit trail for a model version, and reusing it
here would either collide with it or silently conflate two different kinds
of row). "Build fully in memory, insert once" persistence discipline (mirrors
``RECOMMENDATION_GENERATION``'s own precedent) -- BACKTEST is pure
computation over already-persisted, read-only data with no other
side-effecting steps to sequence, so there is no benefit to the
create-early-RUNNING-row discipline ``GAMEWEEK_REVIEW``/the optimizer jobs
use: on any failure below, nothing has been written yet, so a clean
``JobResult(status=FAILED, ...)`` with no partial ``backtest_runs`` row is
always correct -- see ``packages.domain.learning.BacktestRunStatus``'s own
docstring.

``ctx.parameters`` requires ``{"model_version_id": "<uuid>"}`` -- unlike the
forward-looking optimizer/training jobs, there is no sensible default (which
model would "the" backtest even mean?), so a caller must always say exactly
which already-trained ``model_versions`` row to backtest.
"""

import math
import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import joblib
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.jobs.registry import register_handler
from app.jobs.runner import JobContext, JobResult, JobRunner
from app.modeling import baselines
from app.modeling import candidates as candidate_models
from app.modeling import metrics as metric_fns
from app.modeling.champion import aggregate_mae, aggregate_spearman
from app.modeling.walk_forward import Fold, build_expanding_window_folds, usable_season_labels_with_features
from packages.database.models.backtest_run import BacktestRun
from packages.database.models.model_version import ModelVersion
from packages.database.models.player_features import PlayerFeatures
from packages.database.models.player_gameweek_stats import PlayerGameweekStats
from packages.database.models.season import Season
from packages.database.session import SessionLocal
from packages.domain.jobs import JobStatus, JobType
from packages.domain.learning import BacktestRunStatus
from packages.domain.modeling import BASELINE_MODEL_FAMILIES, ModelFamily

# Deliberately duplicated (not imported) from app.jobs.handlers.model_training's own
# module-private _BASELINE_PREDICT_FNS / app.modeling.shadow_evaluation's own
# _BASELINE_FEATURE_ROW_PREDICT_FNS -- same duplication precedent already established by that
# latter module's own docstring (module-private names aren't imported cross-module in this
# codebase; each caller keeps its own small, obviously-identical copy).
_BASELINE_FEATURE_ROW_PREDICT_FNS = {
    ModelFamily.BASELINE_SEASON_AVERAGE: baselines.predict_season_average,
    ModelFamily.BASELINE_RECENT_AVERAGE: baselines.predict_recent_average,
    ModelFamily.BASELINE_WEIGHTED_RECENT_AVERAGE: baselines.predict_weighted_recent_average,
}


def _to_decimal(value: float | None) -> Decimal | None:
    """Round to 4 decimal places and convert to ``Decimal`` -- mirrors every other Numeric(*, 4)
    column in this codebase's own ``Decimal(str(round(value, 4)))`` convention (e.g.
    ``app.learning.signals``, ``app.jobs.handlers.model_training``'s own ``ModelMetric`` persistence).
    ``None`` passes through unchanged -- a genuinely undefined aggregate (e.g. Spearman when every
    fold's own value was undefined) is never coerced to a placeholder number.
    """
    return Decimal(str(round(value, 4))) if value is not None else None


@register_handler(JobType.BACKTEST)
class BacktestJobRunner(JobRunner):
    """Runs BACKTEST for one already-trained ``model_version_id``: fresh walk-forward folds over the
    CURRENT dataset, scored by that model's own fixed, already-fitted artifact/formula -- never refit.
    See module docstring for the full scope and persistence rationale.
    """

    def run(self, ctx: JobContext) -> JobResult:
        session = SessionLocal()
        try:
            raw_model_version_id = (ctx.parameters or {}).get("model_version_id")
            try:
                model_version_id = uuid.UUID(str(raw_model_version_id))
            except (ValueError, AttributeError, TypeError):
                return JobResult(
                    status=JobStatus.FAILED,
                    output=None,
                    error_code="INVALID_MODEL_VERSION_ID",
                    error_message="parameters.model_version_id is required and must be a valid UUID.",
                )

            model_version = session.get(ModelVersion, model_version_id)
            if model_version is None:
                return JobResult(
                    status=JobStatus.FAILED,
                    output=None,
                    error_code="MODEL_VERSION_NOT_FOUND",
                    error_message=f"model_versions row {model_version_id} not found.",
                )

            feature_version = model_version.feature_version
            usable_seasons = usable_season_labels_with_features(session, feature_version)
            if not usable_seasons:
                return JobResult(
                    status=JobStatus.FAILED,
                    output=None,
                    error_code="NO_SEASONS_WITH_FEATURES",
                    error_message=(
                        f"cannot backtest -- no season has any player_features rows for "
                        f"feature_version={feature_version}."
                    ),
                )

            sklearn_model: Any | None = None
            if model_version.model_family not in BASELINE_MODEL_FAMILIES:
                if model_version.artifact_uri is None:
                    return JobResult(
                        status=JobStatus.FAILED,
                        output=None,
                        error_code="MODEL_ARTIFACT_MISSING",
                        error_message=(
                            f"model_versions row {model_version_id} "
                            f"(family={model_version.model_family.value}) has no artifact_uri to "
                            "backtest against."
                        ),
                    )
                try:
                    sklearn_model = joblib.load(Path(model_version.artifact_uri))
                except Exception as exc:  # noqa: BLE001 - a corrupt/missing artifact is a clean job failure
                    return JobResult(
                        status=JobStatus.FAILED,
                        output=None,
                        error_code="MODEL_ARTIFACT_LOAD_FAILED",
                        error_message=str(exc),
                    )

            folds = build_expanding_window_folds(usable_seasons)

            started_at = datetime.now(UTC)
            fold_metrics: list[dict[str, Any]] = []
            for fold in folds:
                fold_metric = self._score_fold_against_fixed_model(
                    session, fold, feature_version, model_version.model_family, sklearn_model
                )
                if fold_metric is not None:
                    fold_metrics.append(fold_metric)

            if not fold_metrics:
                return JobResult(
                    status=JobStatus.FAILED,
                    output=None,
                    error_code="NO_FOLD_SCORED",
                    error_message=(
                        "no fold produced a single successfully-scored observation for this "
                        "model_version_id against the current dataset."
                    ),
                )

            completed_at = datetime.now(UTC)
            aggregate = self._aggregate(fold_metrics)
            n_observations = sum(fm["n_observations"] for fm in fold_metrics)
            degenerate_single_season = len(folds) == 1 and folds[0].is_degenerate_single_season

            row = BacktestRun(
                job_id=uuid.UUID(ctx.job_id),
                model_version_id=model_version.id,
                feature_version=feature_version,
                run_status=BacktestRunStatus.SUCCEEDED,
                usable_seasons_json=usable_seasons,
                fold_count=len(folds),
                degenerate_single_season=degenerate_single_season,
                fold_metrics_json=fold_metrics,
                aggregate_mae=_to_decimal(aggregate["mae"]),
                aggregate_rmse=_to_decimal(aggregate["rmse"]),
                aggregate_mean_bias=_to_decimal(aggregate["mean_bias"]),
                aggregate_spearman=_to_decimal(aggregate["spearman"]),
                aggregate_top_20_capture_rate=_to_decimal(aggregate["top_20_capture_rate"]),
                n_observations=n_observations,
                started_at=started_at,
                completed_at=completed_at,
            )
            session.add(row)
            session.commit()
            session.refresh(row)

            return JobResult(
                status=JobStatus.SUCCEEDED,
                output={
                    "backtest_run_id": str(row.id),
                    "model_version_id": str(model_version.id),
                    "model_family": model_version.model_family.value,
                    "feature_version": feature_version,
                    "usable_seasons": usable_seasons,
                    "fold_count": len(folds),
                    "degenerate_single_season": degenerate_single_season,
                    "aggregate_mae": aggregate["mae"],
                    "aggregate_rmse": aggregate["rmse"],
                    "aggregate_spearman": aggregate["spearman"],
                    "n_observations": n_observations,
                },
                error_code=None,
                error_message=None,
            )
        except Exception as exc:  # noqa: BLE001 - job row must always reach a terminal status
            session.rollback()
            return JobResult(
                status=JobStatus.FAILED, output=None, error_code="BACKTEST_FAILED", error_message=str(exc)
            )
        finally:
            session.close()

    # --- Per-fold scoring against the fixed, already-trained model ------------------------------------

    def _load_validation_rows(
        self, session: Session, season_label: str, feature_version: int
    ) -> list[PlayerFeatures]:
        season_id = session.execute(select(Season.id).where(Season.label == season_label)).scalar_one()
        return list(
            session.execute(
                select(PlayerFeatures).where(
                    PlayerFeatures.season_id == season_id,
                    PlayerFeatures.feature_version == feature_version,
                )
            )
            .scalars()
            .all()
        )

    def _preload_xp_estimates(
        self, session: Session, rows: Sequence[PlayerFeatures]
    ) -> dict[tuple[uuid.UUID, uuid.UUID], list]:
        """One bulk query for every (player_id, gameweek_id) pair this fold needs -- avoids an N+1
        query, mirroring ``app.jobs.handlers.model_training``'s own identically-shaped preload.
        """
        player_ids = {row.player_id for row in rows}
        gameweek_ids = {row.gameweek_id for row in rows}
        if not player_ids or not gameweek_ids:
            return {}
        stats_rows = session.execute(
            select(
                PlayerGameweekStats.player_id,
                PlayerGameweekStats.gameweek_id,
                PlayerGameweekStats.xp_estimate,
            ).where(
                PlayerGameweekStats.player_id.in_(player_ids),
                PlayerGameweekStats.gameweek_id.in_(gameweek_ids),
            )
        ).all()
        result: dict[tuple[uuid.UUID, uuid.UUID], list] = defaultdict(list)
        for stats_row in stats_rows:
            result[(stats_row.player_id, stats_row.gameweek_id)].append(stats_row.xp_estimate)
        return result

    def _predict_fixed_model(
        self,
        session: Session,
        model_family: ModelFamily,
        sklearn_model: Any | None,
        playable_rows: Sequence[PlayerFeatures],
    ) -> list[float | None]:
        """Predict ``playable_rows`` with the FIXED, already-trained model -- never fits/refits anything."""
        if model_family in BASELINE_MODEL_FAMILIES:
            if model_family == ModelFamily.BASELINE_RULES_EP_NEXT:
                xp_estimates = self._preload_xp_estimates(session, playable_rows)
                return [
                    baselines.sum_known_xp_estimates(xp_estimates.get((row.player_id, row.gameweek_id), []))
                    for row in playable_rows
                ]
            predict_fn = _BASELINE_FEATURE_ROW_PREDICT_FNS[model_family]
            return [predict_fn(row) for row in playable_rows]

        assert sklearn_model is not None  # invariant: only baseline families ever have sklearn_model=None
        x_val, _ = candidate_models.build_feature_matrix(playable_rows)
        if x_val.empty:
            return [None] * len(playable_rows)
        x_aligned = candidate_models.align_columns(x_val, list(sklearn_model.feature_names_in_))
        predictions = sklearn_model.predict(x_aligned)
        return [float(value) for value in predictions]

    def _score_fold_against_fixed_model(
        self,
        session: Session,
        fold: Fold,
        feature_version: int,
        model_family: ModelFamily,
        sklearn_model: Any | None,
    ) -> dict[str, Any] | None:
        """Score one fold's validation season against the fixed model -- ``fold.train_season_labels``
        is deliberately never used here (see module docstring: nothing is ever refit).

        Returns ``None`` (never recorded) if this fold's validation season has
        no playable rows yet, or the fixed model produces zero usable
        predictions for it -- mirrors ``app.jobs.handlers.model_training``'s
        own "nothing to score this fold against" graceful skip, never
        synthesizing a fold result from zero observations.
        """
        rows = self._load_validation_rows(session, fold.validation_season_label, feature_version)
        playable = [row for row in rows if row.target_total_points is not None]
        if not playable:
            return None

        y_true = [float(row.target_total_points) for row in playable if row.target_total_points is not None]
        y_pred = self._predict_fixed_model(session, model_family, sklearn_model, playable)

        paired = [(t, p) for t, p in zip(y_true, y_pred, strict=True) if p is not None and not math.isnan(p)]
        if not paired:
            return None
        filtered_true = [t for t, _ in paired]
        filtered_pred = [p for _, p in paired]

        return {
            "fold_index": fold.fold_index,
            "validation_season_label": fold.validation_season_label,
            "mae": metric_fns.mean_absolute_error(filtered_true, filtered_pred),
            "rmse": metric_fns.root_mean_squared_error(filtered_true, filtered_pred),
            "mean_bias": metric_fns.mean_bias(filtered_true, filtered_pred),
            "spearman": metric_fns.spearman_rank_correlation(filtered_true, filtered_pred),
            "top_20_capture_rate": metric_fns.top_k_capture_rate(filtered_true, filtered_pred, k=20),
            "calibration_buckets": metric_fns.calibration_buckets(filtered_true, filtered_pred),
            "n_observations": len(paired),
        }

    @staticmethod
    def _aggregate(fold_metrics: list[dict[str, Any]]) -> dict[str, float | None]:
        """Simple mean-across-folds aggregation -- reuses ``app.modeling.champion``'s own
        ``aggregate_mae``/``aggregate_spearman`` for every metric (not just MAE/Spearman
        themselves), mirroring ``app.jobs.handlers.model_training``'s own identical reuse:
        mechanically each is just "a simple mean across folds", so a second, near-duplicate
        averaging helper isn't worth writing.
        """
        return {
            "mae": aggregate_mae([fm["mae"] for fm in fold_metrics]),
            "rmse": aggregate_mae([fm["rmse"] for fm in fold_metrics]),
            "mean_bias": aggregate_mae([fm["mean_bias"] for fm in fold_metrics]),
            "spearman": aggregate_spearman([fm["spearman"] for fm in fold_metrics]),
            "top_20_capture_rate": aggregate_spearman([fm["top_20_capture_rate"] for fm in fold_metrics]),
        }
