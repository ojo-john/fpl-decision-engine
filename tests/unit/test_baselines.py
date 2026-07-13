"""Unit tests for app.modeling.baselines: the 3 pure feature-row-lookup baselines.

No database, no network — hand-constructed stand-ins satisfying
``FeatureRowLike`` are used in place of real ``PlayerFeatures`` rows.
``predict_rules_based_ep_next`` (the 4th baseline) is DB-touching and is
covered instead by the integration test
(``tests/integration/test_model_training_job.py``).
"""

from dataclasses import dataclass
from decimal import Decimal

from app.modeling.baselines import (
    predict_recent_average,
    predict_season_average,
    predict_weighted_recent_average,
)


@dataclass
class _FeatureRowDouble:
    season_avg_points_to_date: Decimal | float | None = None
    career_avg_points_prior_seasons: Decimal | float | None = None
    rolling_avg_points_5gw: Decimal | float | None = None
    form_weighted_avg_points: Decimal | float | None = None


# --- predict_season_average -------------------------------------------


def test_predict_season_average_prefers_season_avg_when_present():
    row = _FeatureRowDouble(
        season_avg_points_to_date=Decimal("5.50"), career_avg_points_prior_seasons=Decimal("3.00")
    )
    assert predict_season_average(row) == 5.5


def test_predict_season_average_falls_back_to_career_average_when_season_avg_missing():
    row = _FeatureRowDouble(season_avg_points_to_date=None, career_avg_points_prior_seasons=Decimal("3.25"))
    assert predict_season_average(row) == 3.25


def test_predict_season_average_returns_none_when_both_missing():
    row = _FeatureRowDouble()
    assert predict_season_average(row) is None


# --- predict_recent_average ---------------------------------------------


def test_predict_recent_average_returns_rolling_5gw():
    row = _FeatureRowDouble(rolling_avg_points_5gw=Decimal("4.20"))
    assert predict_recent_average(row) == 4.2


def test_predict_recent_average_returns_none_when_missing():
    row = _FeatureRowDouble()
    assert predict_recent_average(row) is None


# --- predict_weighted_recent_average ------------------------------------


def test_predict_weighted_recent_average_returns_form_weighted():
    row = _FeatureRowDouble(form_weighted_avg_points=Decimal("6.75"))
    assert predict_weighted_recent_average(row) == 6.75


def test_predict_weighted_recent_average_returns_none_when_missing():
    row = _FeatureRowDouble()
    assert predict_weighted_recent_average(row) is None
