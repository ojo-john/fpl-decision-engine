"""Unit tests (no DB) for app.modeling.start_probability: feature/label construction.

Mirrors tests/unit/test_candidates_feature_matrix.py's own no-DB, in-memory-PlayerFeatures style.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.modeling.start_probability import (
    FEATURE_COLUMNS,
    START_LABEL_MINUTES_THRESHOLD,
    build_start_probability_feature_matrix,
    build_start_probability_feature_row,
)
from packages.database.models.player_features import PlayerFeatures


def _make_row(*, player_id=None, gameweek_id=None, target_total_points=0, **overrides) -> PlayerFeatures:
    values = dict(
        id=uuid.uuid4(),
        player_id=player_id or uuid.uuid4(),
        season_id=uuid.uuid4(),
        gameweek_id=gameweek_id or uuid.uuid4(),
        feature_version=1,
        cutoff_timestamp=datetime(2026, 8, 1, tzinfo=UTC),
        computed_at=datetime(2026, 8, 1, tzinfo=UTC),
        target_total_points=target_total_points,
        position_short_name="MID",
        is_missing_history=False,
        rolling_avg_minutes_3gw=Decimal("60.00"),
        data_source="test",
    )
    values.update(overrides)
    return PlayerFeatures(**values)


def test_build_feature_matrix_excludes_rows_with_no_label_yet():
    """Same "target gameweek not finished yet" filter as
    app.modeling.candidates.build_feature_matrix -- a row with target_total_points=None must never
    be included, regardless of minutes data."""
    player_id, gameweek_id = uuid.uuid4(), uuid.uuid4()
    not_yet_played = _make_row(player_id=player_id, gameweek_id=gameweek_id, target_total_points=None)

    x, y = build_start_probability_feature_matrix([not_yet_played], {(player_id, gameweek_id): 90})

    assert x.empty
    assert y.empty


def test_build_feature_matrix_label_is_one_when_minutes_meet_threshold():
    player_id, gameweek_id = uuid.uuid4(), uuid.uuid4()
    row = _make_row(player_id=player_id, gameweek_id=gameweek_id, target_total_points=6)

    x, y = build_start_probability_feature_matrix(
        [row], {(player_id, gameweek_id): START_LABEL_MINUTES_THRESHOLD}
    )

    assert list(y) == [1.0]
    assert len(x) == 1


def test_build_feature_matrix_label_is_zero_when_minutes_below_threshold():
    player_id, gameweek_id = uuid.uuid4(), uuid.uuid4()
    row = _make_row(player_id=player_id, gameweek_id=gameweek_id, target_total_points=1)

    x, y = build_start_probability_feature_matrix(
        [row], {(player_id, gameweek_id): START_LABEL_MINUTES_THRESHOLD - 1}
    )

    assert list(y) == [0.0]


def test_build_feature_matrix_absent_minutes_entry_is_a_real_zero_label():
    """A (player_id, gameweek_id) pair absent from minutes_by_player_gameweek means zero
    player_gameweek_stats rows exist for it -- a real, valid "did not feature" observation for a
    gameweek that HAS finished (target_total_points is not None), not missing data. See the
    module's own docstring."""
    player_id, gameweek_id = uuid.uuid4(), uuid.uuid4()
    row = _make_row(player_id=player_id, gameweek_id=gameweek_id, target_total_points=0)

    x, y = build_start_probability_feature_matrix([row], {})

    assert list(y) == [0.0]


def test_build_feature_matrix_null_feature_values_become_nan_not_zero():
    """None means genuinely unknown -- mirrors app.modeling.candidates' own identical convention
    (never silently defaulted to 0.0)."""
    player_id, gameweek_id = uuid.uuid4(), uuid.uuid4()
    row = _make_row(
        player_id=player_id,
        gameweek_id=gameweek_id,
        target_total_points=2,
        rolling_avg_minutes_3gw=None,
    )

    x, _ = build_start_probability_feature_matrix([row], {(player_id, gameweek_id): 90})

    assert x["rolling_avg_minutes_3gw"].isna().all()


def test_build_feature_matrix_double_gameweek_minutes_are_summed_by_the_caller():
    """This function trusts whatever total minutes_by_player_gameweek already provides for a
    (player_id, gameweek_id) key -- the SUMMING across a Double Gameweek's two fixture rows is the
    caller's (app.jobs.handlers.start_probability_training's bulk preload) job, mirroring
    app.jobs.handlers.model_training's own xp_estimates preload split of responsibility. This test
    only proves the threshold comparison itself is inclusive (>=, not >)."""
    player_id, gameweek_id = uuid.uuid4(), uuid.uuid4()
    row = _make_row(player_id=player_id, gameweek_id=gameweek_id, target_total_points=9)

    x, y = build_start_probability_feature_matrix(
        [row], {(player_id, gameweek_id): START_LABEL_MINUTES_THRESHOLD}
    )

    assert list(y) == [1.0]


def test_feature_columns_all_reach_the_built_matrix():
    """Regression-style test for the exact 'wired in but inert' failure mode a prior gap in this
    codebase had for xG/defensive-contribution features -- proves every FEATURE_COLUMNS name is a
    real column in the built matrix, not just a name that happens to exist on PlayerFeatures."""
    player_id, gameweek_id = uuid.uuid4(), uuid.uuid4()
    row = _make_row(player_id=player_id, gameweek_id=gameweek_id, target_total_points=3)

    x, _ = build_start_probability_feature_matrix([row], {(player_id, gameweek_id): 90})

    for column in FEATURE_COLUMNS:
        assert column in x.columns, f"{column} missing from built training matrix"
    for position in ("position_GKP", "position_DEF", "position_MID", "position_FWD"):
        assert position in x.columns


def test_build_feature_row_never_filters_on_label():
    """Live inference is always for a not-yet-played gameweek (target_total_points is always
    None) -- must never be dropped, mirrors app.modeling.candidates.build_feature_row's own
    identical reasoning."""
    row = _make_row(target_total_points=None)

    x = build_start_probability_feature_row(row)

    assert len(x) == 1


@pytest.mark.parametrize("column", FEATURE_COLUMNS)
def test_build_feature_row_includes_every_curated_column(column):
    row = _make_row(target_total_points=None)
    x = build_start_probability_feature_row(row)
    assert column in x.columns
