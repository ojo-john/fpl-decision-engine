"""Unit tests (no DB) for app.optimization.projection's start-probability adjustment step.

Mirrors tests/unit/test_champion_projection.py's own no-DB style: every DB-touching call
(`compute_features_for_player_gameweek`) is monkeypatched at the module attribute projection.py
actually calls through.
"""

import math
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from app.optimization.projection import (
    START_PROBABILITY_ADJUSTED_SUFFIX,
    PlayerProjection,
    _apply_start_probability_adjustment,
    project_player_with_champion,
)
from app.optimization.start_probability_loader import LoadedStartProbabilityModel
from packages.database.models.player import Player
from packages.database.models.player_features import PlayerFeatures

_TARGET_GAMEWEEK_ID = uuid.uuid4()


@pytest.fixture(autouse=True)
def _no_notes_by_default(monkeypatch):
    monkeypatch.setattr("app.optimization.projection.get_active_availability_risk_note", lambda *a, **k: None)
    monkeypatch.setattr("app.optimization.projection.get_active_role_boost_note", lambda *a, **k: None)


def _make_player(**overrides) -> Player:
    defaults = dict(
        id=uuid.uuid4(),
        fpl_element_id=1,
        season_id=uuid.uuid4(),
        now_cost=50,
        status="a",
        chance_of_playing_next_round=None,
        ep_next=Decimal("8.0"),
        points_per_game=None,
    )
    defaults.update(overrides)
    return Player(**defaults)


def _make_feature_row() -> PlayerFeatures:
    return PlayerFeatures(
        id=uuid.uuid4(),
        player_id=uuid.uuid4(),
        season_id=uuid.uuid4(),
        gameweek_id=_TARGET_GAMEWEEK_ID,
        feature_version=1,
        cutoff_timestamp=datetime(2026, 8, 1, tzinfo=UTC),
        computed_at=datetime(2026, 8, 1, tzinfo=UTC),
        target_total_points=None,
        position_short_name="MID",
        is_missing_history=False,
        rolling_avg_minutes_3gw=Decimal("60.00"),
        rolling_avg_minutes_5gw=Decimal("55.00"),
        data_source="test",
    )


class _FakeSklearnModel:
    """Fakes just enough of a fitted classifier's surface for align_columns + predict_proba."""

    def __init__(self, p_start: float):
        self._p_start = p_start
        self.feature_names_in_ = np.array(
            [
                "rolling_avg_points_3gw",
                "rolling_avg_points_5gw",
                "rolling_avg_points_10gw",
                "rolling_avg_minutes_3gw",
                "rolling_avg_minutes_5gw",
                "season_avg_points_to_date",
                "rest_days",
                "is_missing_history",
                "position_GKP",
                "position_DEF",
                "position_MID",
                "position_FWD",
            ]
        )

    def predict_proba(self, x: pd.DataFrame) -> np.ndarray:
        return np.array([[1.0 - self._p_start, self._p_start]])


def _make_loaded_model(p_start: float, feature_version: int = 1) -> LoadedStartProbabilityModel:
    return LoadedStartProbabilityModel(
        start_probability_model_id=uuid.uuid4(),
        feature_version=feature_version,
        sklearn_model=_FakeSklearnModel(p_start),
    )


# --- project_player_with_champion: default (start_probability_model=None) is a true no-op -------


def test_project_player_with_champion_default_start_probability_model_is_unchanged(monkeypatch):
    """The highest-priority guarantee this whole mechanism makes: omitting
    start_probability_model (every pre-existing caller) must produce byte-identical behavior to
    before this feature existed -- no session call, no suffix, no exception."""
    player = _make_player(ep_next=Decimal("8.0"))

    def _boom(*args, **kwargs):
        raise AssertionError("compute_features_for_player_gameweek must not be called")

    monkeypatch.setattr("app.optimization.projection.compute_features_for_player_gameweek", _boom)

    result = project_player_with_champion(
        session=None, player=player, target_gameweek_id=_TARGET_GAMEWEEK_ID, champion=None
    )

    assert result.source == "ep_next"
    assert not result.source.endswith(START_PROBABILITY_ADJUSTED_SUFFIX)
    assert result.projected_points == 8.0


# --- _apply_start_probability_adjustment: direct unit tests --------------------------------------


def test_apply_start_probability_adjustment_scales_projection_and_appends_suffix(monkeypatch):
    monkeypatch.setattr(
        "app.optimization.projection.compute_features_for_player_gameweek",
        lambda *a, **k: _make_feature_row(),
    )
    player = _make_player()
    base = PlayerProjection(player.id, 10.0, "ep_next", is_eligible=True)
    loaded_model = _make_loaded_model(p_start=0.4)

    result = _apply_start_probability_adjustment(
        session=None,
        player=player,
        target_gameweek_id=_TARGET_GAMEWEEK_ID,
        start_probability_model=loaded_model,
        projection=base,
    )

    assert result.projected_points == pytest.approx(4.0)
    assert result.source == f"ep_next{START_PROBABILITY_ADJUSTED_SUFFIX}"
    assert result.is_eligible is True


def test_apply_start_probability_adjustment_near_certain_starter_barely_moves_projection(monkeypatch):
    monkeypatch.setattr(
        "app.optimization.projection.compute_features_for_player_gameweek",
        lambda *a, **k: _make_feature_row(),
    )
    player = _make_player()
    base = PlayerProjection(player.id, 10.0, "ep_next", is_eligible=True)
    loaded_model = _make_loaded_model(p_start=0.98)

    result = _apply_start_probability_adjustment(
        session=None,
        player=player,
        target_gameweek_id=_TARGET_GAMEWEEK_ID,
        start_probability_model=loaded_model,
        projection=base,
    )

    assert result.projected_points == pytest.approx(9.8)


def test_apply_start_probability_adjustment_none_model_is_noop():
    player = _make_player()
    base = PlayerProjection(player.id, 10.0, "ep_next", is_eligible=True)

    result = _apply_start_probability_adjustment(
        session=None,
        player=player,
        target_gameweek_id=_TARGET_GAMEWEEK_ID,
        start_probability_model=None,
        projection=base,
    )

    assert result is base


def test_apply_start_probability_adjustment_ineligible_projection_is_noop(monkeypatch):
    """A hard-excluded (0-point, is_eligible=False) projection must never be scaled -- mirrors
    _apply_availability_risk_notes/_apply_role_boost_notes's own identical guard."""

    def _boom(*args, **kwargs):
        raise AssertionError("must not compute features for an already-ineligible projection")

    monkeypatch.setattr("app.optimization.projection.compute_features_for_player_gameweek", _boom)
    player = _make_player()
    base = PlayerProjection(player.id, 0.0, "unavailable_status_excluded", is_eligible=False)
    loaded_model = _make_loaded_model(p_start=0.5)

    result = _apply_start_probability_adjustment(
        session=None,
        player=player,
        target_gameweek_id=_TARGET_GAMEWEEK_ID,
        start_probability_model=loaded_model,
        projection=base,
    )

    assert result is base


def test_apply_start_probability_adjustment_feature_computation_failure_degrades_gracefully(monkeypatch):
    def _raise(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.optimization.projection.compute_features_for_player_gameweek", _raise)
    player = _make_player()
    base = PlayerProjection(player.id, 10.0, "ep_next", is_eligible=True)
    loaded_model = _make_loaded_model(p_start=0.4)

    result = _apply_start_probability_adjustment(
        session=None,
        player=player,
        target_gameweek_id=_TARGET_GAMEWEEK_ID,
        start_probability_model=loaded_model,
        projection=base,
    )

    assert result is base


def test_apply_start_probability_adjustment_nan_prediction_degrades_gracefully(monkeypatch):
    monkeypatch.setattr(
        "app.optimization.projection.compute_features_for_player_gameweek",
        lambda *a, **k: _make_feature_row(),
    )
    player = _make_player()
    base = PlayerProjection(player.id, 10.0, "ep_next", is_eligible=True)
    loaded_model = _make_loaded_model(p_start=math.nan)

    result = _apply_start_probability_adjustment(
        session=None,
        player=player,
        target_gameweek_id=_TARGET_GAMEWEEK_ID,
        start_probability_model=loaded_model,
        projection=base,
    )

    assert result is base


# --- End-to-end through project_player_with_champion: composes with notes -----------------------


def test_project_player_with_champion_start_probability_composes_with_role_note(monkeypatch):
    """Proves the fixed application order (start-probability adjustment, THEN notes) by checking
    both suffixes appear, in that order, and both multipliers actually compound."""
    monkeypatch.setattr(
        "app.optimization.projection.compute_features_for_player_gameweek",
        lambda *a, **k: _make_feature_row(),
    )

    class _FakeRoleNote:
        severity = Decimal("1.5")

    monkeypatch.setattr(
        "app.optimization.projection.get_active_role_boost_note", lambda *a, **k: _FakeRoleNote()
    )

    player = _make_player(ep_next=Decimal("10.0"))
    loaded_model = _make_loaded_model(p_start=0.5)

    result = project_player_with_champion(
        session=None,
        player=player,
        target_gameweek_id=_TARGET_GAMEWEEK_ID,
        champion=None,
        start_probability_model=loaded_model,
    )

    assert result.source == f"ep_next{START_PROBABILITY_ADJUSTED_SUFFIX}_role_boosted"
    assert result.projected_points == pytest.approx(10.0 * 0.5 * 1.5)
