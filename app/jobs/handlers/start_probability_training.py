"""START_PROBABILITY_MODEL_TRAINING job: trains a SEPARATE, additive auxiliary classifier --
P(player starts this gameweek) -- never a retrain of the main points-regression champion.

Why an auxiliary model, not a change to MODEL_TRAINING's own champion
------------------------------------------------------------------------
The main ``GRADIENT_BOOSTING`` champion (``model_versions``, ``is_champion=True``) is live and
load-bearing for real squad decisions. Re-architecting IT to also predict minutes/starts (e.g. a
staged classification-then-regression pipeline) would mean retraining and re-promoting the one
artifact every optimizer call already depends on -- risky to do casually. Instead, this job trains
a completely separate, small classifier, persisted in its own ``start_probability_models`` table
(see that model's own docstring), with its own ``is_active`` flag that starts ``False`` and is
NEVER flipped by this job. Activating a freshly-trained row to actually influence live projections
(``app.optimization.projection.project_player_with_champion``, via
``app.optimization.start_probability_loader.load_active_start_probability_model``) is left as a
deliberate, separate, human-reviewed decision -- this job's whole job is to produce the real,
walk-forward-backtested evidence (``aggregate_auc``/``aggregate_accuracy``/``aggregate_f1``/
``aggregate_log_loss``) that decision should be based on.

This directly replaces what's currently done by hand for rotation risk: manually-typed
``player_availability_risk_notes`` severities reasoned from prose scouting reports (e.g. real
notes this season for Woltemade/Osula) with a learned signal built from the same rolling
minutes/points history already computed for the main model. If/when a row here is activated, it
composes with (does not replace) those notes -- see ``app.optimization.projection`` for exactly
where in the multiplier-stacking order it applies.

Mechanically mirrors ``app/jobs/handlers/model_training.py`` closely: reuses
``app.modeling.walk_forward``'s exact fold-building
(``usable_season_labels_with_features``/``build_expanding_window_folds``, including its own
single-season graceful degradation), fits a FRESH classifier per fold (never carries a fitted
model across folds), then refits ONE final classifier on every usable row combined for the
deployed artifact -- same "walk-forward measures generalization, the deployed artifact uses
maximum available signal" reasoning as ``model_training.py``'s own final-artifact refit.

``ctx.parameters`` accepts ``{"feature_version": 1}`` (defaults to
``app.features.service.FEATURE_VERSION`` if omitted) -- same convention as ``MODEL_TRAINING``.
"""

import uuid
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.features.service import FEATURE_VERSION
from app.jobs.registry import register_handler
from app.jobs.runner import JobContext, JobResult, JobRunner
from app.modeling import metrics as metric_fns
from app.modeling import start_probability as sp
from app.modeling.candidates import align_columns, select_non_degenerate_columns
from app.modeling.champion import aggregate_mae, aggregate_spearman
from app.modeling.walk_forward import Fold, build_expanding_window_folds, usable_season_labels_with_features
from packages.database.models.player_features import PlayerFeatures
from packages.database.models.player_gameweek_stats import PlayerGameweekStats
from packages.database.models.season import Season
from packages.database.models.start_probability_model import StartProbabilityModel
from packages.database.session import SessionLocal
from packages.domain.jobs import JobStatus, JobType

# Where trained artifacts are saved -- see .gitignore's `models/*` / `!models/.gitkeep` entry;
# generated artifacts are never committed. A distinct subdirectory prefix from
# app.jobs.handlers.model_training's own MODELS_DIR usage (f"{family}_{trained_at}") so the two
# job types' artifacts never collide on disk despite sharing the same parent directory.
MODELS_DIR = Path("models")


def _to_decimal(value: float | None):
    from decimal import Decimal

    return Decimal(str(round(value, 4))) if value is not None else None


@register_handler(JobType.START_PROBABILITY_MODEL_TRAINING)
class StartProbabilityModelTrainingJobRunner(JobRunner):
    """Runs START_PROBABILITY_MODEL_TRAINING for one feature_version. See module docstring."""

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
                        f"cannot train -- no season has any player_features rows for "
                        f"feature_version={feature_version} (run FEATURE_GENERATION first)"
                    ),
                )

            folds = build_expanding_window_folds(usable_seasons)

            rows_by_season = self._load_feature_rows_by_season(session, usable_seasons, feature_version)
            minutes_by_player_gameweek = self._preload_minutes(session, rows_by_season)

            trained_at = datetime.now(UTC)
            job_id = uuid.UUID(ctx.job_id)

            fold_metrics: list[dict[str, Any]] = []
            for fold in folds:
                fold_metric = self._score_fold(fold, rows_by_season, minutes_by_player_gameweek)
                if fold_metric is not None:
                    fold_metrics.append(fold_metric)

            if not fold_metrics:
                return JobResult(
                    status=JobStatus.FAILED,
                    output=None,
                    error_code="NO_FOLD_SCORED",
                    error_message="no fold produced a single successfully-scored observation.",
                )

            all_rows = [row for rows in rows_by_season.values() for row in rows]
            artifact_info = self._fit_and_save_final_artifact(
                all_rows, minutes_by_player_gameweek, trained_at
            )

            degenerate_single_season = len(folds) == 1 and folds[0].is_degenerate_single_season
            n_observations = sum(fm["n_observations"] for fm in fold_metrics)

            row = StartProbabilityModel(
                feature_version=feature_version,
                target_variable=f"started_minutes_ge_{sp.START_LABEL_MINUTES_THRESHOLD}",
                training_seasons_json=usable_seasons,
                usable_seasons_json=usable_seasons,
                hyperparameters_json={
                    "max_iter": sp.GRADIENT_BOOSTING_MAX_ITER,
                    "max_depth": sp.GRADIENT_BOOSTING_MAX_DEPTH,
                    "learning_rate": sp.GRADIENT_BOOSTING_LEARNING_RATE,
                    "random_state": sp.RANDOM_STATE,
                },
                artifact_uri=artifact_info["artifact_uri"],
                artifact_checksum_sha256=artifact_info["artifact_checksum_sha256"],
                aggregate_auc=_to_decimal(aggregate_spearman([fm["auc"] for fm in fold_metrics])),
                aggregate_accuracy=_to_decimal(aggregate_mae([fm["accuracy"] for fm in fold_metrics])),
                aggregate_f1=_to_decimal(aggregate_mae([fm["f1"] for fm in fold_metrics])),
                aggregate_log_loss=_to_decimal(aggregate_spearman([fm["log_loss"] for fm in fold_metrics])),
                n_observations=n_observations,
                fold_metrics_json=fold_metrics,
                degenerate_single_season=degenerate_single_season,
                is_active=False,
                trained_at=trained_at,
                training_job_id=job_id,
            )
            session.add(row)
            session.commit()
            session.refresh(row)

            return JobResult(
                status=JobStatus.SUCCEEDED,
                output={
                    "start_probability_model_id": str(row.id),
                    "feature_version": feature_version,
                    "usable_seasons": usable_seasons,
                    "fold_count": len(folds),
                    "degenerate_single_season": degenerate_single_season,
                    "aggregate_auc": float(row.aggregate_auc) if row.aggregate_auc is not None else None,
                    "aggregate_accuracy": float(row.aggregate_accuracy)
                    if row.aggregate_accuracy is not None
                    else None,
                    "aggregate_f1": float(row.aggregate_f1) if row.aggregate_f1 is not None else None,
                    "aggregate_log_loss": float(row.aggregate_log_loss)
                    if row.aggregate_log_loss is not None
                    else None,
                    "n_observations": n_observations,
                    "is_active": False,
                },
                error_code=None,
                error_message=None,
            )
        except Exception as exc:  # noqa: BLE001 - job row must always reach a terminal status
            session.rollback()
            return JobResult(
                status=JobStatus.FAILED,
                output=None,
                error_code="START_PROBABILITY_MODEL_TRAINING_FAILED",
                error_message=str(exc),
            )
        finally:
            session.close()

    # --- Data preloading -----------------------------------------------

    def _load_feature_rows_by_season(
        self, session: Session, season_labels: list[str], feature_version: int
    ) -> dict[str, list[PlayerFeatures]]:
        """Identical shape to app.jobs.handlers.model_training's own same-named method."""
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

    def _preload_minutes(
        self, session: Session, rows_by_season: dict[str, list[PlayerFeatures]]
    ) -> dict[tuple[uuid.UUID, uuid.UUID], int]:
        """One bulk query for every (player_id, gameweek_id) pair any row needs, summing minutes
        across fixture rows (Double Gameweek handling -- see app.modeling.start_probability's own
        module docstring) -- avoids an N+1 query, mirrors
        app.jobs.handlers.model_training._preload_xp_estimates's own identically-shaped preload.
        """
        player_ids = {row.player_id for rows in rows_by_season.values() for row in rows}
        gameweek_ids = {row.gameweek_id for rows in rows_by_season.values() for row in rows}
        if not player_ids or not gameweek_ids:
            return {}
        stats_rows = session.execute(
            select(
                PlayerGameweekStats.player_id,
                PlayerGameweekStats.gameweek_id,
                PlayerGameweekStats.minutes,
            ).where(
                PlayerGameweekStats.player_id.in_(player_ids),
                PlayerGameweekStats.gameweek_id.in_(gameweek_ids),
            )
        ).all()
        result: dict[tuple[uuid.UUID, uuid.UUID], int] = defaultdict(int)
        for stats_row in stats_rows:
            result[(stats_row.player_id, stats_row.gameweek_id)] += stats_row.minutes
        return result

    # --- Fold scoring -----------------------------------------------------

    def _score_fold(
        self,
        fold: Fold,
        rows_by_season: dict[str, list[PlayerFeatures]],
        minutes_by_player_gameweek: dict[tuple[uuid.UUID, uuid.UUID], int],
    ) -> dict[str, Any] | None:
        train_rows = [row for label in fold.train_season_labels for row in rows_by_season[label]]
        validation_rows = rows_by_season[fold.validation_season_label]

        x_train, y_train = sp.build_start_probability_feature_matrix(train_rows, minutes_by_player_gameweek)
        x_val, y_val = sp.build_start_probability_feature_matrix(validation_rows, minutes_by_player_gameweek)
        if x_train.empty or x_val.empty:
            return None
        # A fold whose TRAIN split has only one label present cannot be fit meaningfully (a
        # classifier needs both classes to learn a real decision boundary) -- skip rather than
        # fit a degenerate always-one-class model, mirroring
        # app.modeling.candidates.select_non_degenerate_columns's own "degenerate input, skip
        # rather than fail downstream" reasoning one level up (rows, not columns, here).
        if y_train.nunique() < 2:
            return None

        train_columns = select_non_degenerate_columns(x_train)
        if not train_columns:
            return None
        x_train = x_train[train_columns]
        x_val = align_columns(x_val, train_columns)

        model = sp.build_start_probability_classifier()
        model.fit(x_train, y_train)

        y_pred_proba = model.predict_proba(x_val)[:, 1]
        y_pred_label = (y_pred_proba >= 0.5).astype(float)
        y_true = y_val.to_numpy()

        return {
            "fold_index": fold.fold_index,
            "validation_season_label": fold.validation_season_label,
            "auc": metric_fns.binary_auc(y_true, y_pred_proba),
            "accuracy": metric_fns.binary_accuracy(y_true, y_pred_label),
            "f1": metric_fns.binary_f1(y_true, y_pred_label),
            "log_loss": metric_fns.binary_log_loss(y_true, y_pred_proba),
            "n_observations": int(len(y_true)),
            "positive_rate": float(y_true.mean()) if len(y_true) else None,
        }

    def _fit_and_save_final_artifact(
        self,
        all_rows: list[PlayerFeatures],
        minutes_by_player_gameweek: dict[tuple[uuid.UUID, uuid.UUID], int],
        trained_at: datetime,
    ) -> dict[str, Any]:
        """Refit on every playable row across every usable season combined -- same
        "walk-forward measures generalization, the deployed artifact uses maximum available
        signal" reasoning as app.jobs.handlers.model_training's own final-champion-artifact refit.
        """
        from packages.fpl_client.fingerprint import sha256_hex

        x_all, y_all = sp.build_start_probability_feature_matrix(all_rows, minutes_by_player_gameweek)
        x_all = x_all[select_non_degenerate_columns(x_all)]

        model = sp.build_start_probability_classifier()
        model.fit(x_all, y_all)

        model_dir = MODELS_DIR / f"start_probability_{trained_at:%Y%m%dT%H%M%SZ}"
        model_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = model_dir / "model.joblib"
        joblib.dump(model, artifact_path)

        checksum = sha256_hex(artifact_path.read_bytes())
        return {"artifact_uri": str(artifact_path), "artifact_checksum_sha256": checksum}
