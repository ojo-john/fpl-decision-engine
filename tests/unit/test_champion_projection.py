"""Unit tests for app.optimization.projection.project_player_with_champion (no DB).

Every DB-touching step (`compute_features_for_player_gameweek`,
`baselines.predict_rules_based_ep_next`) is monkeypatched at the module
attribute projection.py actually calls through, so these tests never open a
session — mirroring the no-DB style of tests/unit/test_projection.py, which
this module extends rather than replaces.
"""

import math
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.optimization.champion_loader import LoadedChampion
from app.optimization.projection import project_player, project_player_with_champion
from packages.database.models.player import Player
from packages.database.models.player_features import PlayerFeatures
from packages.domain.modeling import ModelFamily

_TARGET_GAMEWEEK_ID = uuid.uuid4()


def _make_player(
    *,
    status: str | None = "a",
    chance_of_playing_next_round: int | None = None,
    ep_next: str | None = "8.0",
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


def _make_feature_row(player: Player, **overrides) -> PlayerFeatures:
    values = {
        "player_id": player.id,
        "season_id": player.season_id,
        "gameweek_id": _TARGET_GAMEWEEK_ID,
        "feature_version": 1,
        "cutoff_timestamp": datetime.now(UTC),
        "computed_at": datetime.now(UTC),
        "target_total_points": None,
        "position_short_name": "MID",
        "data_source": "test",
        "is_missing_history": False,
    }
    values.update(overrides)
    return PlayerFeatures(**values)


def _make_champion(model_family: ModelFamily, sklearn_model=None, feature_version: int = 1) -> LoadedChampion:
    return LoadedChampion(
        model_version_id=uuid.uuid4(),
        model_family=model_family,
        feature_version=feature_version,
        sklearn_model=sklearn_model,
    )


class _StubTreeModel:
    """A minimal stand-in for a fitted scikit-learn regressor."""

    def __init__(self, feature_names: list[str], prediction: float):
        self.feature_names_in_ = feature_names
        self._prediction = prediction
        self.received_columns: list[str] | None = None

    def predict(self, x):
        self.received_columns = list(x.columns)
        return [self._prediction]


# --- 1. Hard-excluded statuses short-circuit before any model is consulted ---


@pytest.mark.parametrize("status", ["i", "s", "u"])
def test_hard_excluded_status_never_reaches_model(monkeypatch, status):
    player = _make_player(status=status, ep_next="99.9")
    # A champion that would return an absurdly high prediction if consulted —
    # proves exclusion happens BEFORE any model/feature-computation code runs.
    stub_model = _StubTreeModel(feature_names=["season_avg_points_to_date"], prediction=999.0)
    champion = _make_champion(ModelFamily.DECISION_TREE, sklearn_model=stub_model)

    def _boom(*args, **kwargs):
        raise AssertionError("feature computation must never be reached for a hard-excluded status")

    monkeypatch.setattr("app.optimization.projection.compute_features_for_player_gameweek", _boom)

    result = project_player_with_champion(None, player, _TARGET_GAMEWEEK_ID, champion)

    assert result.is_eligible is False
    assert result.projected_points == 0.0
    assert result.source == "unavailable_status_excluded"
    assert stub_model.received_columns is None


# --- 2. champion=None falls back to project_player exactly ---


def test_champion_none_falls_back_to_project_player_for_eligible_player():
    player = _make_player(status="a", ep_next="8.8")
    result = project_player_with_champion(None, player, _TARGET_GAMEWEEK_ID, None)
    expected = project_player(player)
    assert result == expected
    assert result.source == "ep_next"


def test_champion_none_falls_back_to_project_player_for_excluded_player():
    player = _make_player(status="i", ep_next="99.9")
    result = project_player_with_champion(None, player, _TARGET_GAMEWEEK_ID, None)
    expected = project_player(player)
    assert result == expected
    assert result.is_eligible is False


# --- 3. Feature computation raising falls back to project_player ---


def test_feature_computation_raising_falls_back(monkeypatch):
    player = _make_player(status="a", ep_next="8.8")
    champion = _make_champion(ModelFamily.BASELINE_SEASON_AVERAGE)
    monkeypatch.setattr("app.optimization.projection._is_cold_start_season", lambda *a, **k: False)

    def _raise(*args, **kwargs):
        raise ValueError("no fixture context for this brand-new player")

    monkeypatch.setattr("app.optimization.projection.compute_features_for_player_gameweek", _raise)

    result = project_player_with_champion(None, player, _TARGET_GAMEWEEK_ID, champion)

    assert result == project_player(player)
    assert result.source == "ep_next"


# --- 4. Baseline champion whose lookup returns None falls back per-player ---


def test_baseline_champion_none_lookup_falls_back_for_this_player(monkeypatch):
    player = _make_player(status="a", ep_next="8.8")
    champion = _make_champion(ModelFamily.BASELINE_SEASON_AVERAGE)
    monkeypatch.setattr("app.optimization.projection._is_cold_start_season", lambda *a, **k: False)
    # A feature row with no season/career average at all -- a genuinely
    # brand-new player, per predict_season_average's own documented None case.
    feature_row = _make_feature_row(
        player, season_avg_points_to_date=None, career_avg_points_prior_seasons=None
    )
    monkeypatch.setattr(
        "app.optimization.projection.compute_features_for_player_gameweek",
        lambda *a, **k: feature_row,
    )

    result = project_player_with_champion(None, player, _TARGET_GAMEWEEK_ID, champion)

    assert result == project_player(player)
    assert result.source == "ep_next"


# --- 5. Baseline champion with a real value: source-tagged ml_champion_* ---


def test_baseline_champion_with_value_is_source_tagged(monkeypatch):
    player = _make_player(status="a", ep_next="8.8")
    champion = _make_champion(ModelFamily.BASELINE_SEASON_AVERAGE)
    monkeypatch.setattr("app.optimization.projection._is_cold_start_season", lambda *a, **k: False)
    feature_row = _make_feature_row(player, season_avg_points_to_date=Decimal("6.50"))
    monkeypatch.setattr(
        "app.optimization.projection.compute_features_for_player_gameweek",
        lambda *a, **k: feature_row,
    )

    result = project_player_with_champion(None, player, _TARGET_GAMEWEEK_ID, champion)

    assert result.is_eligible is True
    assert result.projected_points == pytest.approx(6.50)
    assert result.source == "ml_champion_baseline_season_average"


def test_rules_ep_next_baseline_champion_calls_baseline_lookup(monkeypatch):
    player = _make_player(status="a", ep_next="8.8")
    champion = _make_champion(ModelFamily.BASELINE_RULES_EP_NEXT)
    monkeypatch.setattr("app.optimization.projection._is_cold_start_season", lambda *a, **k: False)
    feature_row = _make_feature_row(player)
    monkeypatch.setattr(
        "app.optimization.projection.compute_features_for_player_gameweek",
        lambda *a, **k: feature_row,
    )
    monkeypatch.setattr(
        "app.modeling.baselines.predict_rules_based_ep_next",
        lambda session, player_id, gameweek_id: 4.2,
    )

    result = project_player_with_champion(None, player, _TARGET_GAMEWEEK_ID, champion)

    assert result.projected_points == pytest.approx(4.2)
    assert result.source == "ml_champion_baseline_rules_ep_next"


# --- 6. Tree-family champion with a mocked sklearn model ---


def test_tree_champion_returns_mocked_prediction(monkeypatch):
    player = _make_player(status="a", ep_next="8.8")
    stub_model = _StubTreeModel(
        feature_names=["season_avg_points_to_date", "position_GKP", "position_DEF", "position_MID"],
        prediction=7.25,
    )
    champion = _make_champion(ModelFamily.RANDOM_FOREST, sklearn_model=stub_model)
    monkeypatch.setattr("app.optimization.projection._is_cold_start_season", lambda *a, **k: False)
    feature_row = _make_feature_row(player, season_avg_points_to_date=Decimal("6.50"))
    monkeypatch.setattr(
        "app.optimization.projection.compute_features_for_player_gameweek",
        lambda *a, **k: feature_row,
    )

    result = project_player_with_champion(None, player, _TARGET_GAMEWEEK_ID, champion)

    assert result.is_eligible is True
    assert result.projected_points == pytest.approx(7.25)
    assert result.source == "ml_champion_random_forest"
    assert stub_model.received_columns == stub_model.feature_names_in_


def test_tree_champion_nan_prediction_falls_back(monkeypatch):
    player = _make_player(status="a", ep_next="8.8")
    stub_model = _StubTreeModel(feature_names=["season_avg_points_to_date"], prediction=math.nan)
    champion = _make_champion(ModelFamily.DECISION_TREE, sklearn_model=stub_model)
    monkeypatch.setattr("app.optimization.projection._is_cold_start_season", lambda *a, **k: False)
    feature_row = _make_feature_row(player)
    monkeypatch.setattr(
        "app.optimization.projection.compute_features_for_player_gameweek",
        lambda *a, **k: feature_row,
    )

    result = project_player_with_champion(None, player, _TARGET_GAMEWEEK_ID, champion)

    assert result == project_player(player)
    assert result.source == "ep_next"


# --- 7. Cold-start season: never consults the champion model, blends ep_next with career avg ---


def test_cold_start_season_never_consults_the_champion_model(monkeypatch):
    player = _make_player(status="a", ep_next="8.8")
    # A champion that would return an absurdly high prediction if consulted -- proves the
    # cold-start check routes around the model entirely, mirroring the hard-excluded-status
    # test's own "must never be reached" pattern for the model itself (feature computation DOES
    # still happen in this path now, to source career_avg_points_prior_seasons -- see the blend
    # test below).
    stub_model = _StubTreeModel(feature_names=["season_avg_points_to_date"], prediction=999.0)
    champion = _make_champion(ModelFamily.RANDOM_FOREST, sklearn_model=stub_model)
    monkeypatch.setattr("app.optimization.projection._is_cold_start_season", lambda *a, **k: True)
    monkeypatch.setattr(
        "app.optimization.projection.compute_features_for_player_gameweek",
        lambda *a, **k: _make_feature_row(player, career_avg_points_prior_seasons=None),
    )

    result = project_player_with_champion(None, player, _TARGET_GAMEWEEK_ID, champion)

    assert result == project_player(player)
    assert result.source == "ep_next"
    assert stub_model.received_columns is None


def test_cold_start_season_blends_ep_next_with_career_average_when_present(monkeypatch):
    player = _make_player(status="a", ep_next="4.0")
    champion = _make_champion(ModelFamily.RANDOM_FOREST, sklearn_model=_StubTreeModel([], 999.0))
    monkeypatch.setattr("app.optimization.projection._is_cold_start_season", lambda *a, **k: True)
    monkeypatch.setattr(
        "app.optimization.projection.compute_features_for_player_gameweek",
        lambda *a, **k: _make_feature_row(player, career_avg_points_prior_seasons=Decimal("6.36")),
    )

    result = project_player_with_champion(None, player, _TARGET_GAMEWEEK_ID, champion)

    assert result.is_eligible is True
    assert result.projected_points == pytest.approx((4.0 + 6.36) / 2)
    assert result.source == "cold_start_blend_ep_next_and_career_avg"


def test_cold_start_season_falls_back_to_ep_next_alone_with_no_career_history(monkeypatch):
    """A brand-new-to-FPL player: no earlier-season row exists to blend with at all."""
    player = _make_player(status="a", ep_next="4.0")
    champion = _make_champion(ModelFamily.RANDOM_FOREST, sklearn_model=_StubTreeModel([], 999.0))
    monkeypatch.setattr("app.optimization.projection._is_cold_start_season", lambda *a, **k: True)
    monkeypatch.setattr(
        "app.optimization.projection.compute_features_for_player_gameweek",
        lambda *a, **k: _make_feature_row(player, career_avg_points_prior_seasons=None),
    )

    result = project_player_with_champion(None, player, _TARGET_GAMEWEEK_ID, champion)

    assert result == project_player(player)
    assert result.source == "ep_next"


def test_cold_start_season_feature_computation_raising_falls_back_to_ep_next_alone(monkeypatch):
    player = _make_player(status="a", ep_next="4.0")
    champion = _make_champion(ModelFamily.RANDOM_FOREST, sklearn_model=_StubTreeModel([], 999.0))
    monkeypatch.setattr("app.optimization.projection._is_cold_start_season", lambda *a, **k: True)

    def _raise(*args, **kwargs):
        raise ValueError("no fixture context")

    monkeypatch.setattr("app.optimization.projection.compute_features_for_player_gameweek", _raise)

    result = project_player_with_champion(None, player, _TARGET_GAMEWEEK_ID, champion)

    assert result == project_player(player)
    assert result.source == "ep_next"
