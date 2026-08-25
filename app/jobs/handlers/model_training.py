"""MODEL_TRAINING job: Milestone 3 Layer 3 — walk-forward validation, champion selection, artifacts.

Orchestrates, for one ``feature_version``:

1. Determine which seasons actually have ``player_features`` rows for that
   version (``app.modeling.walk_forward.usable_season_labels_with_features``
   — never hardcoded).
2. Build season-level expanding-window walk-forward folds
   (``app.modeling.walk_forward.build_expanding_window_folds`` — gracefully
   degrades to a single in-sample fold if only one season is usable yet).
3. For every fold, score all 7 candidates (4 pure-lookup baselines —
   ``app.modeling.baselines`` — plus 3 scikit-learn tree regressors —
   ``app.modeling.candidates``) against that fold's validation season, using
   the shared metric functions in ``app.modeling.metrics``.
4. Persist one ``ModelVersion`` row per candidate (this run's own fresh audit
   snapshot — never an upsert-in-place; see that model's docstring) plus one
   ``ModelMetric`` row per fold each candidate was successfully scored on,
   plus one aggregate (``fold_index IS NULL``) row per candidate.
5. Select a champion (lowest aggregate MAE, tie-broken by highest aggregate
   Spearman — ``app.modeling.champion``). If the champion is a tree family,
   refit ONE final model on every playable row across every usable season
   combined (not any single fold's train split — walk-forward validation's
   job was only to pick which family generalizes best; the deployed artifact
   should use the maximum available signal) and serialize it via joblib.
6. BOOTSTRAP CASE ONLY (no ``model_versions`` row currently has
   ``is_champion=True`` anywhere in the table): enforce the "exactly one
   ``model_versions`` row has ``is_champion=True`` across the whole table"
   invariant (``app.modeling.champion.set_exactly_one_champion``), considering
   every row ever inserted, not just this run's 7, and flag the winner
   ``lifecycle_state = CHAMPION`` immediately. This step's behavior is
   UNCHANGED from before Milestone 7 Layer 5.

Milestone 7 Layer 5 (Recursive Learning Loop: Challenger Training/Shadow-Mode
Evaluation/Promotion Gates/Rollback) changes step 6 above for the NON-bootstrap
case: if a champion ALREADY exists when this run finishes, the winning
candidate does NOT get promoted immediately -- ``set_exactly_one_champion`` is
NOT called, ``is_champion`` stays ``False`` on every one of this run's 7 rows,
and the winner instead gets ``lifecycle_state = SHADOW_CHALLENGER``. This
run's winner becomes a CHALLENGER, not a champion: real promotion only
happens later, once ``app.modeling.shadow_evaluation
.compute_shadow_challenger_evaluation`` has scored it against the live
champion over enough real, finished gameweeks, via
``app.modeling.promotion.evaluate_promotion``/``decide_and_record_promotion``.
This is a small, surgical change to this one decision point -- everything
else in this handler (folds, scoring, persistence, artifact fitting) is
unchanged.

``ctx.parameters`` accepts ``{"feature_version": 1}`` (defaults to
``app.features.service.FEATURE_VERSION`` if omitted).

Target variable is always ``"total_points"`` for this milestone's scope (see
``app/modeling/__init__.py``'s module docstring for why a direct
single-target regression, not a staged pipeline, was chosen).
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
from app.modeling import baselines
from app.modeling import candidates as candidate_models
from app.modeling import metrics as metric_fns
from app.modeling.champion import (
    CandidateMetrics,
    aggregate_mae,
    aggregate_spearman,
    select_champion,
    set_exactly_one_champion,
)
from app.modeling.walk_forward import (
    Fold,
    build_expanding_window_folds,
    usable_season_labels_with_features,
)
from packages.database.models.model_metric import ModelMetric
from packages.database.models.model_version import ModelVersion
from packages.database.models.player_features import PlayerFeatures
from packages.database.models.player_gameweek_stats import PlayerGameweekStats
from packages.database.models.season import Season
from packages.database.session import SessionLocal
from packages.domain.jobs import JobStatus, JobType
from packages.domain.learning import ModelLifecycleState
from packages.domain.modeling import BASELINE_MODEL_FAMILIES, ModelFamily
from packages.fpl_client.fingerprint import sha256_hex

TARGET_VARIABLE = "total_points"

# Where champion tree artifacts are saved — see .gitignore's `models/*` /
# `!models/.gitkeep` entry (added in Milestone 3 Layer 1): generated
# artifacts are never committed, only the directory placeholder is.
MODELS_DIR = Path("models")

_BASELINE_PREDICT_FNS = {
    ModelFamily.BASELINE_SEASON_AVERAGE: baselines.predict_season_average,
    ModelFamily.BASELINE_RECENT_AVERAGE: baselines.predict_recent_average,
    ModelFamily.BASELINE_WEIGHTED_RECENT_AVERAGE: baselines.predict_weighted_recent_average,
}

_CANDIDATE_BUILDERS = {
    ModelFamily.DECISION_TREE: candidate_models.build_decision_tree,
    ModelFamily.RANDOM_FOREST: candidate_models.build_random_forest,
    ModelFamily.GRADIENT_BOOSTING: candidate_models.build_gradient_boosting,
}

_TREE_HYPERPARAMETERS: dict[ModelFamily, dict[str, Any]] = {
    ModelFamily.DECISION_TREE: {
        "max_depth": candidate_models.DECISION_TREE_MAX_DEPTH,
        "random_state": candidate_models.RANDOM_STATE,
    },
    ModelFamily.RANDOM_FOREST: {
        "n_estimators": candidate_models.RANDOM_FOREST_N_ESTIMATORS,
        "max_depth": candidate_models.RANDOM_FOREST_MAX_DEPTH,
        "random_state": candidate_models.RANDOM_STATE,
    },
    ModelFamily.GRADIENT_BOOSTING: {
        "max_iter": candidate_models.GRADIENT_BOOSTING_MAX_ITER,
        "max_depth": candidate_models.GRADIENT_BOOSTING_MAX_DEPTH,
        "learning_rate": candidate_models.GRADIENT_BOOSTING_LEARNING_RATE,
        "random_state": candidate_models.RANDOM_STATE,
    },
}


@register_handler(JobType.MODEL_TRAINING)
class ModelTrainingJobRunner(JobRunner):
    """Runs MODEL_TRAINING for one feature_version: walk-forward validation across all 7 candidates."""

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
            xp_estimates = self._preload_xp_estimates(session, rows_by_season)

            trained_at = datetime.now(UTC)
            job_id = uuid.UUID(ctx.job_id)

            # family -> list of per-fold metric dicts (only successfully-scored folds)
            fold_results: dict[ModelFamily, list[dict[str, Any]]] = {family: [] for family in ModelFamily}

            for fold in folds:
                self._score_fold(fold, rows_by_season, xp_estimates, fold_results)

            model_version_by_family = self._persist_model_versions_and_metrics(
                session,
                fold_results=fold_results,
                feature_version=feature_version,
                usable_seasons=usable_seasons,
                trained_at=trained_at,
                job_id=job_id,
            )

            eligible = [
                CandidateMetrics(
                    name=family.value,
                    fold_mae=[f["mae"] for f in results],
                    fold_spearman=[f["spearman"] for f in results],
                )
                for family, results in fold_results.items()
                if results
            ]
            if not eligible:
                session.rollback()
                return JobResult(
                    status=JobStatus.FAILED,
                    output=None,
                    error_code="NO_CANDIDATE_SCORED",
                    error_message="no candidate produced a single successfully-scored fold",
                )

            champion_family = ModelFamily(select_champion(eligible))
            champion_version = model_version_by_family[champion_family]

            artifact_info: dict[str, Any] | None = None
            if champion_family not in BASELINE_MODEL_FAMILIES:
                artifact_info = self._fit_and_save_champion_artifact(
                    champion_family, rows_by_season, trained_at
                )
                champion_version.artifact_uri = artifact_info["artifact_uri"]
                champion_version.artifact_bytes = artifact_info["artifact_bytes"]
                champion_version.artifact_checksum_sha256 = artifact_info["artifact_checksum_sha256"]

            # Milestone 7 Layer 5: bootstrap case (no existing champion anywhere in the table) promotes
            # immediately, exactly as before. Once a champion already exists, this run's winner becomes
            # a SHADOW_CHALLENGER instead -- real promotion happens later via
            # app.modeling.promotion.evaluate_promotion/decide_and_record_promotion. See this module's
            # own docstring for the full rationale.
            champion_exists = (
                session.execute(select(ModelVersion.id).where(ModelVersion.is_champion.is_(True)).limit(1))
                .scalars()
                .first()
                is not None
            )
            if champion_exists:
                champion_version.lifecycle_state = ModelLifecycleState.SHADOW_CHALLENGER
                promotion_outcome = "shadow_challenger"
            else:
                all_versions = session.execute(select(ModelVersion)).scalars().all()
                set_exactly_one_champion(all_versions, champion_version)
                champion_version.lifecycle_state = ModelLifecycleState.CHAMPION
                promotion_outcome = "champion"

            session.commit()

            output = {
                "feature_version": feature_version,
                "usable_seasons": usable_seasons,
                "fold_count": len(folds),
                "degenerate_single_season": len(folds) == 1 and folds[0].is_degenerate_single_season,
                "champion": {
                    "model_family": champion_family.value,
                    "model_version_id": str(champion_version.id),
                    "artifact_uri": champion_version.artifact_uri,
                    "promotion_outcome": promotion_outcome,
                    "lifecycle_state": champion_version.lifecycle_state.value,
                },
                "candidates": {
                    family.value: {
                        "model_version_id": str(model_version_by_family[family].id),
                        "fold_count_scored": len(results),
                        "aggregate_mae": aggregate_mae([f["mae"] for f in results]) if results else None,
                        "aggregate_spearman": aggregate_spearman([f["spearman"] for f in results])
                        if results
                        else None,
                    }
                    for family, results in fold_results.items()
                },
            }
            return JobResult(status=JobStatus.SUCCEEDED, output=output, error_code=None, error_message=None)
        except Exception as exc:  # noqa: BLE001 - job row must always reach a terminal status
            session.rollback()
            return JobResult(
                status=JobStatus.FAILED,
                output=None,
                error_code="MODEL_TRAINING_FAILED",
                error_message=str(exc),
            )
        finally:
            session.close()

    # --- Data preloading -----------------------------------------------

    def _load_feature_rows_by_season(
        self, session: Session, season_labels: list[str], feature_version: int
    ) -> dict[str, list[PlayerFeatures]]:
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

    def _preload_xp_estimates(
        self, session: Session, rows_by_season: dict[str, list[PlayerFeatures]]
    ) -> dict[tuple[uuid.UUID, uuid.UUID], list]:
        """One bulk query for every (player_id, gameweek_id) pair any row needs, avoiding an N+1 query."""
        player_ids = {row.player_id for rows in rows_by_season.values() for row in rows}
        gameweek_ids = {row.gameweek_id for rows in rows_by_season.values() for row in rows}
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
        for row in stats_rows:
            result[(row.player_id, row.gameweek_id)].append(row.xp_estimate)
        return result

    # --- Fold scoring -----------------------------------------------------

    def _score_fold(
        self,
        fold: Fold,
        rows_by_season: dict[str, list[PlayerFeatures]],
        xp_estimates: dict[tuple[uuid.UUID, uuid.UUID], list],
        fold_results: dict[ModelFamily, list[dict[str, Any]]],
    ) -> None:
        train_rows = [row for label in fold.train_season_labels for row in rows_by_season[label]]
        validation_rows = rows_by_season[fold.validation_season_label]
        playable = [row for row in validation_rows if row.target_total_points is not None]
        if not playable:
            return  # nothing to score this fold against (e.g. validation season not yet finished)

        y_true = [float(row.target_total_points) for row in playable if row.target_total_points is not None]

        for family, predict_fn in _BASELINE_PREDICT_FNS.items():
            y_pred = [predict_fn(row) for row in playable]
            self._record_fold_metrics(fold_results, family, fold, y_true, y_pred)

        y_pred_ep_next = [
            baselines.sum_known_xp_estimates(xp_estimates.get((row.player_id, row.gameweek_id), []))
            for row in playable
        ]
        self._record_fold_metrics(
            fold_results, ModelFamily.BASELINE_RULES_EP_NEXT, fold, y_true, y_pred_ep_next
        )

        x_train, y_train = candidate_models.build_feature_matrix(train_rows)
        x_val, _ = candidate_models.build_feature_matrix(playable)
        if x_train.empty or x_val.empty:
            return  # no trainable rows this fold (should not happen given the usable-seasons check upstream)

        # Drop any column constant/all-null across this fold's TRAIN split
        # (see select_non_degenerate_columns's docstring for why -- found
        # live via HistGradientBoostingRegressor raising inside numpy's
        # sliding_window_view on a <2-distinct-value column) and align the
        # validation matrix to the exact same column set used to fit.
        train_columns = candidate_models.select_non_degenerate_columns(x_train)
        x_train = x_train[train_columns]
        x_val = candidate_models.align_columns(x_val, train_columns)

        for family, builder in _CANDIDATE_BUILDERS.items():
            model = builder()
            model.fit(x_train, y_train)
            y_pred = model.predict(x_val).tolist()
            self._record_fold_metrics(fold_results, family, fold, y_true, y_pred)

    @staticmethod
    def _record_fold_metrics(
        fold_results: dict[ModelFamily, list[dict[str, Any]]],
        family: ModelFamily,
        fold: Fold,
        y_true: list[float],
        y_pred: list[float | None],
    ) -> None:
        """Filter out rows this candidate couldn't predict (None), then compute and record its fold metrics.

        A candidate that can't produce a prediction for some rows (e.g. a
        baseline with no season/career average yet) is simply not scored on
        those rows — never synthesized to 0 — so ``n_observations`` can
        legitimately differ between candidates for the same fold.
        """
        paired = [(t, p) for t, p in zip(y_true, y_pred, strict=True) if p is not None]
        if not paired:
            return
        filtered_true = [t for t, _ in paired]
        filtered_pred = [p for _, p in paired]

        fold_results[family].append(
            {
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
        )

    # --- Persistence --------------------------------------------------

    def _persist_model_versions_and_metrics(
        self,
        session: Session,
        *,
        fold_results: dict[ModelFamily, list[dict[str, Any]]],
        feature_version: int,
        usable_seasons: list[str],
        trained_at: datetime,
        job_id: uuid.UUID,
    ) -> dict[ModelFamily, ModelVersion]:
        model_version_by_family: dict[ModelFamily, ModelVersion] = {}

        for family in ModelFamily:
            version = ModelVersion(
                model_name=family.value,
                model_family=family,
                feature_version=feature_version,
                target_variable=TARGET_VARIABLE,
                # The full set of seasons available to THIS training run's
                # walk-forward validation -- fold-level detail (which rows a
                # specific fold's estimator actually trained/validated on)
                # lives on model_metrics via fold_index/validation_season_label
                # instead.
                training_seasons_json=usable_seasons,
                hyperparameters_json=_TREE_HYPERPARAMETERS.get(family),
                artifact_uri=None,
                artifact_bytes=None,
                artifact_checksum_sha256=None,
                is_champion=False,
                trained_at=trained_at,
                training_job_id=job_id,
            )
            session.add(version)
            session.flush()  # assign version.id for the ModelMetric FK below
            model_version_by_family[family] = version

            results = fold_results[family]
            for fold_result in results:
                session.add(
                    ModelMetric(
                        model_version_id=version.id,
                        fold_index=fold_result["fold_index"],
                        validation_season_label=fold_result["validation_season_label"],
                        mae=fold_result["mae"],
                        rmse=fold_result["rmse"],
                        mean_bias=fold_result["mean_bias"],
                        spearman_rank_corr=fold_result["spearman"],
                        top_20_capture_rate=fold_result["top_20_capture_rate"],
                        calibration_bucket_json=fold_result["calibration_buckets"],
                        n_observations=fold_result["n_observations"],
                    )
                )

            if results:
                # aggregate_mae/aggregate_spearman are both, mechanically, a
                # simple mean-across-folds (ignoring None for the latter) --
                # reused here for rmse/mean_bias/top_20_capture_rate too
                # rather than duplicating the same one-line average.
                session.add(
                    ModelMetric(
                        model_version_id=version.id,
                        fold_index=None,
                        validation_season_label=None,
                        mae=aggregate_mae([r["mae"] for r in results]),
                        rmse=aggregate_mae([r["rmse"] for r in results]),
                        mean_bias=aggregate_mae([r["mean_bias"] for r in results]),
                        spearman_rank_corr=aggregate_spearman([r["spearman"] for r in results]),
                        top_20_capture_rate=aggregate_spearman([r["top_20_capture_rate"] for r in results]),
                        calibration_bucket_json=None,
                        n_observations=sum(r["n_observations"] for r in results),
                    )
                )

        session.flush()
        return model_version_by_family

    def _fit_and_save_champion_artifact(
        self,
        champion_family: ModelFamily,
        rows_by_season: dict[str, list[PlayerFeatures]],
        trained_at: datetime,
    ) -> dict[str, Any]:
        """Refit the champion tree family on every playable row across every usable season combined.

        Walk-forward validation's job was only to pick which family
        generalizes best across seasons; the deployed artifact should use
        the maximum available signal rather than any single fold's
        (necessarily smaller) train split.
        """
        all_rows = [row for rows in rows_by_season.values() for row in rows]
        x_all, y_all = candidate_models.build_feature_matrix(all_rows)
        x_all = x_all[candidate_models.select_non_degenerate_columns(x_all)]

        model = _CANDIDATE_BUILDERS[champion_family]()
        model.fit(x_all, y_all)

        # Local-dev convenience only, never authoritative -- see ModelVersion.artifact_uri's own
        # docstring: a local filesystem path never survives a real Render redeploy.
        model_dir = MODELS_DIR / f"{champion_family.value}_{trained_at:%Y%m%dT%H%M%SZ}"
        model_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = model_dir / "model.joblib"
        joblib.dump(model, artifact_path)

        artifact_bytes = artifact_path.read_bytes()
        checksum = sha256_hex(artifact_bytes)
        return {
            "artifact_uri": str(artifact_path),
            "artifact_bytes": artifact_bytes,
            "artifact_checksum_sha256": checksum,
        }
