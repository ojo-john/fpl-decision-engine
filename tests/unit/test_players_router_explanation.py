"""Unit tests for apps.api.routers.players._explain_projection (no DB).

The "why this projection" breakdown's plain-English summary branches purely
on ``PlayerProjection.source`` and a few already-computed values -- no DB
access at all, so these are pure-function tests mirroring
tests/unit/test_champion_projection.py's own no-DB style for the formula it
explains.
"""

import uuid

from app.optimization.projection import PlayerProjection
from apps.api.routers.players import _explain_projection


def _projection(source: str, *, is_eligible: bool = True) -> PlayerProjection:
    return PlayerProjection(uuid.uuid4(), 5.0, source, is_eligible=is_eligible)


def test_explain_projection_unavailable_status_excluded():
    projection = _projection("unavailable_status_excluded", is_eligible=False)

    explanation = _explain_projection(
        projection,
        ep_next=None,
        career_avg_points_prior_seasons=None,
        career_avg_early_season_points_prior_seasons=None,
        next_fixture_difficulty=None,
        champion_model_family=None,
    )

    assert "unavailable" in explanation.lower()


def test_explain_projection_cold_start_blend_mentions_both_inputs_and_difficulty():
    projection = _projection("cold_start_blend_ep_next_and_career_avg")

    explanation = _explain_projection(
        projection,
        ep_next=4.0,
        career_avg_points_prior_seasons=6.36,
        career_avg_early_season_points_prior_seasons=None,
        next_fixture_difficulty=2,
        champion_model_family=None,
    )

    assert "4.0" in explanation
    assert "6.36" in explanation
    assert "2/5" in explanation
    assert "new season" in explanation.lower()


def test_explain_projection_cold_start_blend_prefers_early_season_average_when_present():
    projection = _projection("cold_start_blend_ep_next_and_career_avg")

    explanation = _explain_projection(
        projection,
        ep_next=4.0,
        career_avg_points_prior_seasons=6.36,
        career_avg_early_season_points_prior_seasons=10.21,
        next_fixture_difficulty=None,
        champion_model_family=None,
    )

    assert "10.21" in explanation
    assert "6.36" not in explanation
    assert "early-season" in explanation.lower()


def test_explain_projection_cold_start_blend_without_difficulty_omits_difficulty_clause():
    projection = _projection("cold_start_blend_ep_next_and_career_avg")

    explanation = _explain_projection(
        projection,
        ep_next=4.0,
        career_avg_points_prior_seasons=6.36,
        career_avg_early_season_points_prior_seasons=None,
        next_fixture_difficulty=None,
        champion_model_family=None,
    )

    assert "difficulty" not in explanation.lower()


def test_explain_projection_champion_sourced_names_the_model_family():
    projection = _projection("ml_champion_gradient_boosting")

    explanation = _explain_projection(
        projection,
        ep_next=None,
        career_avg_points_prior_seasons=None,
        career_avg_early_season_points_prior_seasons=None,
        next_fixture_difficulty=None,
        champion_model_family="gradient_boosting",
    )

    assert "gradient_boosting" in explanation
    assert "champion" in explanation.lower()


def test_explain_projection_ep_next_fallback():
    projection = _projection("ep_next")

    explanation = _explain_projection(
        projection,
        ep_next=4.0,
        career_avg_points_prior_seasons=None,
        career_avg_early_season_points_prior_seasons=None,
        next_fixture_difficulty=None,
        champion_model_family=None,
    )

    assert "ep_next" in explanation
    assert "scaled" not in explanation.lower()


def test_explain_projection_ep_next_availability_scaled_notes_the_scaling():
    projection = _projection("ep_next_availability_scaled")

    explanation = _explain_projection(
        projection,
        ep_next=4.0,
        career_avg_points_prior_seasons=None,
        career_avg_early_season_points_prior_seasons=None,
        next_fixture_difficulty=None,
        champion_model_family=None,
    )

    assert "scaled down" in explanation.lower()


def test_explain_projection_points_per_game_fallback():
    projection = _projection("points_per_game")

    explanation = _explain_projection(
        projection,
        ep_next=None,
        career_avg_points_prior_seasons=None,
        career_avg_early_season_points_prior_seasons=None,
        next_fixture_difficulty=None,
        champion_model_family=None,
    )

    assert "points-per-game" in explanation.lower()


def test_explain_projection_no_data_zero_falls_back_to_generic_message():
    projection = _projection("no_data_zero")

    explanation = _explain_projection(
        projection,
        ep_next=None,
        career_avg_points_prior_seasons=None,
        career_avg_early_season_points_prior_seasons=None,
        next_fixture_difficulty=None,
        champion_model_family=None,
    )

    assert explanation == "No projection data is available for this player yet."
