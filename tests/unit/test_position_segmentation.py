"""Unit tests for app.modeling.position_segmentation (no DB).

Builds small, synthetic PlayerFeatures rows directly (mirrors
tests/unit/test_champion_projection.py's own "PlayerFeatures(**values), no session" style) and
fits real (tiny, fast) HistGradientBoostingRegressor models against them -- these are genuine
.fit()/.predict() calls, not mocked, since the whole point of this module is real MAE numbers;
what's avoided is only the database, not scikit-learn itself.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from app.modeling.position_segmentation import (
    evaluate_position_segmentation,
    score_fold_global_vs_position_specific,
)
from app.modeling.walk_forward import Fold
from packages.database.models.player_features import PlayerFeatures

_SEASON_A = uuid.uuid4()
_SEASON_B = uuid.uuid4()


def _make_row(
    *,
    season_id: uuid.UUID,
    position: str,
    rolling_avg_points_3gw: float,
    target_total_points: int | None,
) -> PlayerFeatures:
    return PlayerFeatures(
        id=uuid.uuid4(),
        player_id=uuid.uuid4(),
        season_id=season_id,
        gameweek_id=uuid.uuid4(),
        feature_version=1,
        cutoff_timestamp=datetime.now(UTC),
        computed_at=datetime.now(UTC),
        target_total_points=target_total_points,
        position_short_name=position,
        rolling_avg_points_3gw=Decimal(str(rolling_avg_points_3gw)),
        is_missing_history=False,
        data_source="test",
    )


def _synthetic_season_rows(season_id: uuid.UUID, n_per_position: int = 6) -> list[PlayerFeatures]:
    """A small, learnable synthetic season: each position has a distinct points level (GKP lowest,
    FWD highest), plus enough per-row variation in rolling_avg_points_3gw/target_total_points for
    select_non_degenerate_columns to keep the column and for a real .fit() to have something to
    learn from.
    """
    base_by_position = {"GKP": 2.0, "DEF": 3.0, "MID": 4.0, "FWD": 5.0}
    rows = []
    for position, base in base_by_position.items():
        for i in range(n_per_position):
            rows.append(
                _make_row(
                    season_id=season_id,
                    position=position,
                    rolling_avg_points_3gw=base + (i % 3),
                    target_total_points=round(base + (i % 3)),
                )
            )
    return rows


def _make_fold() -> tuple[Fold, dict[str, list[PlayerFeatures]]]:
    rows_by_season = {
        "2020-21": _synthetic_season_rows(_SEASON_A),
        "2021-22": _synthetic_season_rows(_SEASON_B),
    }
    fold = Fold(fold_index=0, train_season_labels=("2020-21",), validation_season_label="2021-22")
    return fold, rows_by_season


def test_score_fold_produces_one_result_per_position_with_data():
    fold, rows_by_season = _make_fold()

    results = score_fold_global_vs_position_specific(fold, rows_by_season)

    positions_scored = {r.position for r in results}
    assert positions_scored == {"GKP", "DEF", "MID", "FWD"}
    for result in results:
        assert result.fold_index == 0
        assert result.validation_season_label == "2021-22"
        assert result.global_mae is not None and result.global_mae >= 0
        assert result.position_specific_mae is not None and result.position_specific_mae >= 0
        assert result.n_observations == 6


def test_score_fold_returns_empty_list_when_validation_season_has_no_playable_rows():
    fold, rows_by_season = _make_fold()
    # Every validation-season row has target_total_points=None -- the gameweek hasn't been played.
    rows_by_season["2021-22"] = [
        _make_row(season_id=_SEASON_B, position="FWD", rolling_avg_points_3gw=5.0, target_total_points=None)
    ]

    results = score_fold_global_vs_position_specific(fold, rows_by_season)

    assert results == []


def test_score_fold_position_specific_mae_is_none_when_that_position_has_no_train_rows():
    """A position present in validation but absent from train rows (should not happen for a real
    season, but defensively handled) still gets a global_mae -- the global model was trained on
    every OTHER position's rows -- but no position_specific_mae, since there was nothing to fit a
    position-specific model on."""
    fold, rows_by_season = _make_fold()
    # Strip every FWD row from the TRAIN season only.
    rows_by_season["2020-21"] = [row for row in rows_by_season["2020-21"] if row.position_short_name != "FWD"]

    results = score_fold_global_vs_position_specific(fold, rows_by_season)

    fwd_result = next(r for r in results if r.position == "FWD")
    assert fwd_result.global_mae is not None
    assert fwd_result.position_specific_mae is None


def test_evaluate_position_segmentation_aggregates_across_folds_and_computes_real_gap():
    fold, rows_by_season = _make_fold()

    result = evaluate_position_segmentation(rows_by_season, [fold], usable_seasons=["2020-21", "2021-22"])

    assert result.usable_seasons == ["2020-21", "2021-22"]
    assert result.fold_count == 1
    assert result.degenerate_single_season is False
    assert set(result.per_position.keys()) == {"GKP", "DEF", "MID", "FWD"}
    for position, values in result.per_position.items():
        assert values["global_mae"] is not None
        assert values["position_specific_mae"] is not None
        assert values["mae_gap_pct"] is not None
        # mae_gap_pct = (global - position_specific) / global -- verify the formula directly.
        expected_gap = (values["global_mae"] - values["position_specific_mae"]) / values["global_mae"]
        assert abs(values["mae_gap_pct"] - expected_gap) < 1e-9
        assert values["n_observations"] == 6
    assert result.overall_global_mae is not None
    assert result.overall_position_specific_mae is not None
    assert result.n_observations == 24  # 4 positions * 6 rows


def test_evaluate_position_segmentation_reports_usable_seasons_verbatim_including_train_only_season():
    """usable_seasons is passed through from the caller, not re-derived from validation labels --
    a train-only season (never any fold's own validation_season_label) must still be reported."""
    fold, rows_by_season = _make_fold()

    result = evaluate_position_segmentation(
        rows_by_season, [fold], usable_seasons=["2019-20", "2020-21", "2021-22"]
    )

    assert result.usable_seasons == ["2019-20", "2020-21", "2021-22"]


def test_evaluate_position_segmentation_handles_zero_folds_gracefully():
    _, rows_by_season = _make_fold()

    result = evaluate_position_segmentation(rows_by_season, [], usable_seasons=["2020-21"])

    assert result.fold_count == 0
    assert result.n_observations == 0
    assert all(values["global_mae"] is None for values in result.per_position.values())
    assert result.overall_global_mae is None
