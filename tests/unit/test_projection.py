"""Unit tests for app.optimization.projection.project_player (table-driven, no DB)."""

import uuid
from decimal import Decimal

import pytest

from app.optimization.projection import project_player
from packages.database.models.player import Player


def _make_player(
    *,
    status: str | None = "a",
    chance_of_playing_next_round: int | None = None,
    ep_next: str | None = None,
    points_per_game: str | None = None,
) -> Player:
    return Player(
        id=uuid.uuid4(),
        fpl_element_id=1,
        season_id=uuid.uuid4(),
        now_cost=50,
        status=status,
        chance_of_playing_next_round=chance_of_playing_next_round,
        ep_next=Decimal(ep_next) if ep_next is not None else None,
        points_per_game=Decimal(points_per_game) if points_per_game is not None else None,
    )


def test_available_with_ep_next_unscaled():
    player = _make_player(status="a", ep_next="8.8")
    result = project_player(player)
    assert result.is_eligible is True
    assert result.projected_points == pytest.approx(8.8)
    assert result.source == "ep_next"


def test_chance_75_boundary_is_unscaled():
    """Scaling triggers strictly below 75, so exactly 75 is untouched."""
    player = _make_player(status="a", chance_of_playing_next_round=75, ep_next="10.0")
    result = project_player(player)
    assert result.projected_points == pytest.approx(10.0)
    assert result.source == "ep_next"


def test_chance_50_scaled_by_half():
    player = _make_player(status="a", chance_of_playing_next_round=50, ep_next="10.0")
    result = project_player(player)
    assert result.projected_points == pytest.approx(5.0)
    assert result.source == "ep_next_availability_scaled"
    assert result.is_eligible is True


def test_ep_next_none_falls_back_to_points_per_game():
    player = _make_player(status="a", ep_next=None, points_per_game="4.4")
    result = project_player(player)
    assert result.projected_points == pytest.approx(4.4)
    assert result.source == "points_per_game"


def test_points_per_game_scaled_when_chance_below_threshold():
    player = _make_player(status="d", chance_of_playing_next_round=25, points_per_game="6.0")
    result = project_player(player)
    assert result.projected_points == pytest.approx(1.5)
    assert result.source == "points_per_game_availability_scaled"
    assert result.is_eligible is True


def test_both_projection_sources_none_yields_no_data_zero():
    player = _make_player(status="a", ep_next=None, points_per_game=None)
    result = project_player(player)
    assert result.projected_points == 0.0
    assert result.source == "no_data_zero"
    assert result.is_eligible is True


@pytest.mark.parametrize("status", ["i", "s", "u"])
def test_hard_excluded_statuses_are_zeroed_and_ineligible(status):
    # Deliberately high fake ep_next proves this is real exclusion, not
    # coincidence of missing data.
    player = _make_player(status=status, ep_next="99.9")
    result = project_player(player)
    assert result.is_eligible is False
    assert result.projected_points == 0.0
    assert result.source == "unavailable_status_excluded"


@pytest.mark.parametrize("status", ["d", "a"])
def test_doubtful_and_available_are_never_hard_excluded(status):
    player = _make_player(status=status, ep_next="5.0")
    result = project_player(player)
    assert result.is_eligible is True
