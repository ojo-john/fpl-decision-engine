"""Unit tests for app.modeling.candidates' feature-matrix construction (no DB).

Focused regression coverage for the exact "wired in but inert" failure mode this repo has hit
before: a column added to PlayerFeatures that never actually reaches the trained model's input
matrix (see app.features.engineering's module docstring on expected_goals prior to
goal_involvement_overperformance_5gw/defensive_contribution_5gw being added as real features).
_feature_record iterates PlayerFeatures.__table__.columns generically, so any new numeric column
should reach build_feature_matrix/build_feature_row automatically -- these tests prove that's
actually true for the two new columns, not just that the feature-engineering functions themselves
return a value.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from app.modeling.candidates import build_feature_matrix, build_feature_row
from packages.database.models.player_features import PlayerFeatures


def _make_feature_row(**overrides) -> PlayerFeatures:
    values = {
        "player_id": uuid.uuid4(),
        "season_id": uuid.uuid4(),
        "gameweek_id": uuid.uuid4(),
        "feature_version": 1,
        "cutoff_timestamp": datetime.now(UTC),
        "computed_at": datetime.now(UTC),
        "target_total_points": 6,
        "position_short_name": "FWD",
        "data_source": "test",
        "is_missing_history": False,
    }
    values.update(overrides)
    return PlayerFeatures(**values)


def test_goal_involvement_overperformance_reaches_training_matrix():
    row = _make_feature_row(goal_involvement_overperformance_5gw=Decimal("1.25"))

    x, _y = build_feature_matrix([row])

    assert "goal_involvement_overperformance_5gw" in x.columns
    assert x["goal_involvement_overperformance_5gw"].iloc[0] == 1.25


def test_defensive_contribution_reaches_training_matrix():
    row = _make_feature_row(defensive_contribution_5gw=Decimal("8.50"))

    x, _y = build_feature_matrix([row])

    assert "defensive_contribution_5gw" in x.columns
    assert x["defensive_contribution_5gw"].iloc[0] == 8.50


def test_both_new_columns_null_encode_as_nan_not_dropped():
    """A row with neither column populated (e.g. schema-drift-null history) must still surface
    both columns as NaN, never silently omitted from the matrix -- matches this module's own
    documented "null passes through as NaN, never imputed" convention for every other feature."""
    row = _make_feature_row()

    x, _y = build_feature_matrix([row])

    assert "goal_involvement_overperformance_5gw" in x.columns
    assert "defensive_contribution_5gw" in x.columns
    assert x["goal_involvement_overperformance_5gw"].isna().iloc[0]
    assert x["defensive_contribution_5gw"].isna().iloc[0]


def test_new_columns_reach_live_inference_single_row_matrix():
    """build_feature_row (live inference, no label filter) must encode the same two columns
    identically to build_feature_matrix -- the two paths share _feature_record precisely so they
    can never silently diverge."""
    row = _make_feature_row(
        target_total_points=None,
        goal_involvement_overperformance_5gw=Decimal("-0.75"),
        defensive_contribution_5gw=Decimal("4.00"),
    )

    x = build_feature_row(row)

    assert x["goal_involvement_overperformance_5gw"].iloc[0] == -0.75
    assert x["defensive_contribution_5gw"].iloc[0] == 4.00
