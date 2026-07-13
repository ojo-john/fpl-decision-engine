"""Unit tests for app.transfers.horizon_projection.compute_horizon_points (no DB).

project_player_with_champion is monkeypatched at the module attribute
horizon_projection.py actually calls through -- mirroring
tests/unit/test_champion_projection.py's exact no-DB mocking convention.
compute_horizon_points is a thin summing wrapper, so these tests only need
to prove the summation itself is correct, not re-verify champion/fallback
selection (already covered by test_champion_projection.py).
"""

import uuid
from decimal import Decimal

import pytest

from app.optimization.projection import PlayerProjection
from app.transfers.horizon_projection import compute_horizon_points
from packages.database.models.player import Player


def _make_player() -> Player:
    return Player(
        id=uuid.uuid4(),
        fpl_element_id=1,
        season_id=uuid.uuid4(),
        now_cost=50,
        status="a",
        ep_next=Decimal("8.0"),
    )


def test_compute_horizon_points_sums_across_every_gameweek(monkeypatch):
    player = _make_player()
    gw1, gw2, gw3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    points_by_gameweek = {gw1: 4.5, gw2: 6.0, gw3: 2.5}

    def _fake_project(session, target_player, target_gameweek_id, champion):
        assert target_player is player
        assert champion is None
        return PlayerProjection(
            player.id, points_by_gameweek[target_gameweek_id], "test_source", is_eligible=True
        )

    monkeypatch.setattr("app.transfers.horizon_projection.project_player_with_champion", _fake_project)

    result = compute_horizon_points(None, player, [gw1, gw2, gw3], None)

    assert result == pytest.approx(4.5 + 6.0 + 2.5)


def test_compute_horizon_points_empty_horizon_is_zero(monkeypatch):
    player = _make_player()

    def _boom(*args, **kwargs):
        raise AssertionError("project_player_with_champion must never be called for an empty horizon")

    monkeypatch.setattr("app.transfers.horizon_projection.project_player_with_champion", _boom)

    result = compute_horizon_points(None, player, [], None)

    assert result == 0.0


def test_compute_horizon_points_calls_once_per_gameweek_not_more(monkeypatch):
    player = _make_player()
    gw1, gw2 = uuid.uuid4(), uuid.uuid4()
    call_count = {"n": 0}

    def _fake_project(session, target_player, target_gameweek_id, champion):
        call_count["n"] += 1
        return PlayerProjection(player.id, 1.0, "test_source", is_eligible=True)

    monkeypatch.setattr("app.transfers.horizon_projection.project_player_with_champion", _fake_project)

    result = compute_horizon_points(None, player, [gw1, gw2], None)

    assert call_count["n"] == 2
    assert result == pytest.approx(2.0)
