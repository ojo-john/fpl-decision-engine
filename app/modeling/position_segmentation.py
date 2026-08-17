"""Position-specific model evaluation: does training one GradientBoosting model PER POSITION
(GKP/DEF/MID/FWD) out-predict the existing single global model (one model, position encoded as a
one-hot feature -- see ``app.modeling.candidates``)?

Closes a gap ``app.modeling.promotion``'s own module docstring named explicitly: "there is no
per-position actuals breakdown computed anywhere in this codebase yet." This module computes that
breakdown for real, via the SAME walk-forward, leakage-safe fold discipline
``app.jobs.handlers.model_training``/``app.jobs.handlers.backtest`` already use
(``app.modeling.walk_forward``'s ``usable_season_labels_with_features``/
``build_expanding_window_folds`` -- reused directly here, never reimplemented).

Deliberately NOT a new training architecture wired into ``MODEL_TRAINING``/``model_versions``/the
promotion pipeline: the position-specific models this module fits are fit fresh, in memory, purely
to compute this evaluation's MAE numbers -- never serialized as artifacts, never given their own
``model_versions`` row, never eligible to become ``is_champion``. Whether to ever build a real
position-specific champion is a downstream decision this module's real numbers can inform, not a
decision this module makes itself. See ``app.jobs.handlers.position_segmentation_validation`` for
the job wrapper and ``packages.database.models.position_segmentation_validation_run`` for how a
run's results are persisted.

Champion-family choice for the "global" side of the comparison: ``HistGradientBoostingRegressor``
(``app.modeling.candidates.build_gradient_boosting``) -- the same family the live champion is drawn
from as of this session (verified against the real dev database's ``model_versions`` table before
writing this module), so the comparison is apples-to-apples against what's ACTUALLY deployed, not
against some other candidate family that happens to also be in the walk-forward mix. The
position-specific side uses the identical family/hyperparameters for the same reason -- any MAE
delta this module finds is attributable to the position-segmentation itself, not to a confounding
choice of a stronger/weaker algorithm on one side.

One-hot position columns become trivially constant (and thus ``select_non_degenerate_columns``-
dropped) inside any single position's own training slice -- no special-casing needed here beyond
reusing that existing column-selection logic exactly as the global path already does.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from app.modeling import candidates as candidate_models
from app.modeling import metrics as metric_fns
from app.modeling.champion import aggregate_mae
from app.modeling.walk_forward import Fold
from packages.database.models.player_features import PlayerFeatures

__all__ = [
    "PositionFoldResult",
    "PositionSegmentationResult",
    "score_fold_global_vs_position_specific",
    "evaluate_position_segmentation",
]


@dataclass(frozen=True)
class PositionFoldResult:
    """One (fold, position) pair's global-vs-position-specific MAE -- the atomic unit this module
    produces; ``evaluate_position_segmentation`` aggregates a list of these across every fold.
    """

    fold_index: int
    validation_season_label: str
    position: str
    # None only if this position had zero playable validation rows in this fold (see
    # score_fold_global_vs_position_specific's own docstring) -- never fabricated as 0.
    global_mae: float | None
    position_specific_mae: float | None
    n_observations: int


def _rows_for_position(rows: Sequence[PlayerFeatures], position: str) -> list[PlayerFeatures]:
    return [row for row in rows if row.position_short_name == position]


def score_fold_global_vs_position_specific(
    fold: Fold, rows_by_season: dict[str, list[PlayerFeatures]]
) -> list[PositionFoldResult]:
    """Score one fold: fit ONE global model on every train-season row (mirrors the real
    ``MODEL_TRAINING``/champion training shape exactly), then for each of the 4 known positions,
    fit a SEPARATE model on only that position's train rows from the same fold -- both scored
    against that position's own slice of this fold's validation rows, so the comparison is always
    apples-to-apples (same validation rows, same fold, same season split).

    Returns one ``PositionFoldResult`` per position that had at least one playable validation row
    this fold (a position entirely absent from this fold's validation season, e.g. a fold whose
    validation season happens to have every GKP row filtered out upstream for some other reason,
    is simply skipped for that position -- never fabricated).

    A position with validation rows but zero TRAIN rows (should not happen in practice -- every
    real season has all 4 positions -- but defensively handled) gets ``position_specific_mae=None``
    for that (fold, position) pair while ``global_mae`` is still computed normally, since the
    global model was trained on every position's rows regardless.
    """
    train_rows = [row for label in fold.train_season_labels for row in rows_by_season[label]]
    validation_rows = rows_by_season[fold.validation_season_label]
    playable = [row for row in validation_rows if row.target_total_points is not None]
    if not playable:
        return []

    x_train, y_train = candidate_models.build_feature_matrix(train_rows)
    if x_train.empty:
        return []
    train_columns = candidate_models.select_non_degenerate_columns(x_train)
    if not train_columns:
        return []
    x_train_aligned = x_train[train_columns]

    global_model = candidate_models.build_gradient_boosting()
    global_model.fit(x_train_aligned, y_train)

    results: list[PositionFoldResult] = []
    for position in candidate_models.KNOWN_POSITIONS:
        position_playable = _rows_for_position(playable, position)
        if not position_playable:
            continue
        y_true = [
            float(row.target_total_points) for row in position_playable if row.target_total_points is not None
        ]

        x_val, _ = candidate_models.build_feature_matrix(position_playable)
        if x_val.empty:
            continue
        x_val_aligned = candidate_models.align_columns(x_val, train_columns)
        global_pred = global_model.predict(x_val_aligned).tolist()
        global_mae = metric_fns.mean_absolute_error(y_true, global_pred)

        position_specific_mae: float | None = None
        position_train_rows = _rows_for_position(train_rows, position)
        if position_train_rows:
            x_pos_train, y_pos_train = candidate_models.build_feature_matrix(position_train_rows)
            if not x_pos_train.empty:
                pos_train_columns = candidate_models.select_non_degenerate_columns(x_pos_train)
                if pos_train_columns:
                    position_model = candidate_models.build_gradient_boosting()
                    position_model.fit(x_pos_train[pos_train_columns], y_pos_train)
                    x_pos_val_aligned = candidate_models.align_columns(x_val, pos_train_columns)
                    position_pred = position_model.predict(x_pos_val_aligned).tolist()
                    position_specific_mae = metric_fns.mean_absolute_error(y_true, position_pred)

        results.append(
            PositionFoldResult(
                fold_index=fold.fold_index,
                validation_season_label=fold.validation_season_label,
                position=position,
                global_mae=global_mae,
                position_specific_mae=position_specific_mae,
                n_observations=len(position_playable),
            )
        )
    return results


@dataclass(frozen=True)
class PositionSegmentationResult:
    """The full, aggregated real-evidence comparison -- what
    ``app.jobs.handlers.position_segmentation_validation`` persists to
    ``position_segmentation_validation_runs``.
    """

    usable_seasons: list[str]
    fold_count: int
    degenerate_single_season: bool
    # position -> {"global_mae", "position_specific_mae", "mae_gap_pct", "n_observations"}.
    # mae_gap_pct = (global_mae - position_specific_mae) / global_mae: positive means the
    # position-specific model beat the global model for that position by that fraction; negative
    # means the global model already handles that position at least as well. None whenever either
    # side has no defined MAE for that position (see PositionFoldResult's own docstring).
    per_position: dict[str, dict[str, float | int | None]]
    overall_global_mae: float | None
    overall_position_specific_mae: float | None
    n_observations: int


def evaluate_position_segmentation(
    rows_by_season: dict[str, list[PlayerFeatures]], folds: list[Fold], usable_seasons: list[str]
) -> PositionSegmentationResult:
    """Aggregate ``score_fold_global_vs_position_specific`` across every fold into one real,
    per-position comparison.

    Aggregation is an unweighted mean across (fold, position) pairs -- mirrors
    ``app.modeling.champion.aggregate_mae``'s own "each fold counts equally, not weighted by
    observation count" philosophy, extended one level: each (season, position) combination counts
    equally too, so a season with unusually many FWD rows doesn't dominate the FWD aggregate more
    than a season with fewer.

    ``usable_seasons`` is passed through verbatim (the caller's own
    ``usable_season_labels_with_features`` result) rather than re-derived from ``rows_by_season``'s
    keys or ``folds``' validation labels -- the earliest usable season is a real TRAIN-only season
    (never any fold's validation label) and must still be reported as usable.
    """
    all_fold_results: list[PositionFoldResult] = []
    for fold in folds:
        all_fold_results.extend(score_fold_global_vs_position_specific(fold, rows_by_season))

    per_position: dict[str, dict[str, float | int | None]] = {}
    for position in candidate_models.KNOWN_POSITIONS:
        position_results = [r for r in all_fold_results if r.position == position]
        global_maes = [r.global_mae for r in position_results if r.global_mae is not None]
        position_maes = [
            r.position_specific_mae for r in position_results if r.position_specific_mae is not None
        ]
        global_mae = aggregate_mae(global_maes) if global_maes else None
        position_mae = aggregate_mae(position_maes) if position_maes else None
        gap_pct = (
            (global_mae - position_mae) / global_mae
            if global_mae is not None and position_mae is not None and global_mae != 0
            else None
        )
        per_position[position] = {
            "global_mae": global_mae,
            "position_specific_mae": position_mae,
            "mae_gap_pct": gap_pct,
            "n_observations": sum(r.n_observations for r in position_results),
        }

    overall_global_maes = [r.global_mae for r in all_fold_results if r.global_mae is not None]
    overall_position_maes = [
        r.position_specific_mae for r in all_fold_results if r.position_specific_mae is not None
    ]

    return PositionSegmentationResult(
        usable_seasons=usable_seasons,
        fold_count=len(folds),
        degenerate_single_season=len(folds) == 1 and folds[0].is_degenerate_single_season,
        per_position=per_position,
        overall_global_mae=aggregate_mae(overall_global_maes) if overall_global_maes else None,
        overall_position_specific_mae=(
            aggregate_mae(overall_position_maes) if overall_position_maes else None
        ),
        n_observations=sum(r.n_observations for r in all_fold_results),
    )
