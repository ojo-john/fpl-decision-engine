"""Unit tests for app.optimization.projection.project_player_with_champion (no DB).

Every DB-touching step (`compute_features_for_player_gameweek`,
`baselines.predict_rules_based_ep_next`, and -- as of the availability-risk-notes
feature -- `get_active_availability_risk_note`, defaulted to "no active note" by
the autouse `_no_availability_risk_note_by_default` fixture below) is
monkeypatched at the module attribute projection.py actually calls through, so
these tests never open a session — mirroring the no-DB style of
tests/unit/test_projection.py, which this module extends rather than replaces.
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


@pytest.fixture(autouse=True)
def _no_availability_risk_note_by_default(monkeypatch):
    """Every test in this file predates player_availability_risk_notes and deliberately never
    opens a session (see module docstring) -- project_player_with_champion's final
    _apply_availability_risk_notes step would otherwise call session.execute(None-session) and
    crash every one of them. Defaulted to "no active note" here; individual tests below override
    this via monkeypatch to exercise the actual stacking behavior."""
    monkeypatch.setattr("app.optimization.projection.get_active_availability_risk_note", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _no_role_boost_note_by_default(monkeypatch):
    """Same rationale as ``_no_availability_risk_note_by_default`` above, for the separate
    player_role_notes mechanism's own ``_apply_role_boost_notes`` step."""
    monkeypatch.setattr("app.optimization.projection.get_active_role_boost_note", lambda *a, **k: None)


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


def test_cold_start_season_prefers_early_season_average_over_full_season_average(monkeypatch):
    """A player whose season-opening form differs from their full-season average (a real,
    well-known slow/fast-starter pattern) should be blended using the early-season-specific
    number, not the full-season one that papers over the difference."""
    player = _make_player(status="a", ep_next="4.0")
    champion = _make_champion(ModelFamily.RANDOM_FOREST, sklearn_model=_StubTreeModel([], 999.0))
    monkeypatch.setattr("app.optimization.projection._is_cold_start_season", lambda *a, **k: True)
    monkeypatch.setattr(
        "app.optimization.projection.compute_features_for_player_gameweek",
        lambda *a, **k: _make_feature_row(
            player,
            career_avg_points_prior_seasons=Decimal("6.36"),
            career_avg_early_season_points_prior_seasons=Decimal("3.00"),
        ),
    )

    result = project_player_with_champion(None, player, _TARGET_GAMEWEEK_ID, champion)

    # Must use the early-season average (3.00), NOT the full-season one (6.36).
    assert result.projected_points == pytest.approx((4.0 + 3.00) / 2)
    assert result.source == "cold_start_blend_ep_next_and_career_avg"


def test_cold_start_season_falls_back_to_full_season_average_with_no_early_season_history(monkeypatch):
    """A player with real prior-season history, but none of it early-season (e.g. every prior
    season they have a record for started with a mid-season transfer) -- must fall back to the
    full-season average rather than treating "no early-season data" as "no data at all"."""
    player = _make_player(status="a", ep_next="4.0")
    champion = _make_champion(ModelFamily.RANDOM_FOREST, sklearn_model=_StubTreeModel([], 999.0))
    monkeypatch.setattr("app.optimization.projection._is_cold_start_season", lambda *a, **k: True)
    monkeypatch.setattr(
        "app.optimization.projection.compute_features_for_player_gameweek",
        lambda *a, **k: _make_feature_row(
            player,
            career_avg_points_prior_seasons=Decimal("6.36"),
            career_avg_early_season_points_prior_seasons=None,
        ),
    )

    result = project_player_with_champion(None, player, _TARGET_GAMEWEEK_ID, champion)

    assert result.projected_points == pytest.approx((4.0 + 6.36) / 2)


def test_cold_start_season_easy_fixture_scales_the_blend_up(monkeypatch):
    player = _make_player(status="a", ep_next="4.0")
    champion = _make_champion(ModelFamily.RANDOM_FOREST, sklearn_model=_StubTreeModel([], 999.0))
    monkeypatch.setattr("app.optimization.projection._is_cold_start_season", lambda *a, **k: True)
    monkeypatch.setattr(
        "app.optimization.projection.compute_features_for_player_gameweek",
        lambda *a, **k: _make_feature_row(
            player, career_avg_points_prior_seasons=Decimal("6.36"), next_fixture_difficulty=1
        ),
    )

    result = project_player_with_champion(None, player, _TARGET_GAMEWEEK_ID, champion)

    base_blend = (4.0 + 6.36) / 2
    assert result.projected_points == pytest.approx(base_blend * 1.15)
    assert result.source == "cold_start_blend_ep_next_and_career_avg"


def test_cold_start_season_hard_fixture_scales_the_blend_down(monkeypatch):
    player = _make_player(status="a", ep_next="4.0")
    champion = _make_champion(ModelFamily.RANDOM_FOREST, sklearn_model=_StubTreeModel([], 999.0))
    monkeypatch.setattr("app.optimization.projection._is_cold_start_season", lambda *a, **k: True)
    monkeypatch.setattr(
        "app.optimization.projection.compute_features_for_player_gameweek",
        lambda *a, **k: _make_feature_row(
            player, career_avg_points_prior_seasons=Decimal("6.36"), next_fixture_difficulty=5
        ),
    )

    result = project_player_with_champion(None, player, _TARGET_GAMEWEEK_ID, champion)

    base_blend = (4.0 + 6.36) / 2
    assert result.projected_points == pytest.approx(base_blend * 0.85)


def test_cold_start_season_neutral_fixture_difficulty_leaves_the_blend_unchanged(monkeypatch):
    player = _make_player(status="a", ep_next="4.0")
    champion = _make_champion(ModelFamily.RANDOM_FOREST, sklearn_model=_StubTreeModel([], 999.0))
    monkeypatch.setattr("app.optimization.projection._is_cold_start_season", lambda *a, **k: True)
    monkeypatch.setattr(
        "app.optimization.projection.compute_features_for_player_gameweek",
        lambda *a, **k: _make_feature_row(
            player, career_avg_points_prior_seasons=Decimal("6.36"), next_fixture_difficulty=3
        ),
    )

    result = project_player_with_champion(None, player, _TARGET_GAMEWEEK_ID, champion)

    assert result.projected_points == pytest.approx((4.0 + 6.36) / 2)


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


# --- Availability-risk-note stacking (app.players... no, app.optimization.projection's own
# _apply_availability_risk_notes) -- these override the module's own autouse fixture default. ---


class _FakeNote:
    def __init__(self, severity, reason="test reason"):
        self.severity = severity
        self.reason = reason


def test_availability_risk_note_discounts_an_eligible_projection(monkeypatch):
    player = _make_player(status="a", ep_next="8.0")
    monkeypatch.setattr(
        "app.optimization.projection.get_active_availability_risk_note",
        lambda *a, **k: _FakeNote(Decimal("0.6")),
    )

    result = project_player_with_champion(None, player, _TARGET_GAMEWEEK_ID, None)

    assert result.projected_points == pytest.approx(8.0 * 0.6)
    assert result.source == "ep_next_availability_risk_noted"
    assert result.is_eligible is True


def test_availability_risk_note_never_applies_to_a_hard_excluded_player(monkeypatch):
    """A note must not resurrect/relabel an already-ineligible (i/s/u status) projection."""
    player = _make_player(status="i", ep_next="99.9")
    monkeypatch.setattr(
        "app.optimization.projection.get_active_availability_risk_note",
        lambda *a, **k: _FakeNote(Decimal("0.6")),
    )

    result = project_player_with_champion(None, player, _TARGET_GAMEWEEK_ID, None)

    assert result.source == "unavailable_status_excluded"
    assert result.projected_points == 0.0
    assert result.is_eligible is False


def test_availability_risk_note_stacks_multiplicatively_with_availability_scaling(monkeypatch):
    """Two independent discounts (FPL's own chance_of_playing scaling, and a flagged risk note)
    must both apply, not one overriding the other."""
    player = _make_player(status="a", ep_next="10.0", chance_of_playing_next_round=50)
    monkeypatch.setattr(
        "app.optimization.projection.get_active_availability_risk_note",
        lambda *a, **k: _FakeNote(Decimal("0.5")),
    )

    result = project_player_with_champion(None, player, _TARGET_GAMEWEEK_ID, None)

    # project_player's own multiplier: 50/100 = 0.5. Then the note's own 0.5 on top.
    assert result.projected_points == pytest.approx(10.0 * 0.5 * 0.5)
    assert result.source == "ep_next_availability_scaled_availability_risk_noted"


def test_no_active_availability_risk_note_leaves_projection_unchanged(monkeypatch):
    """The autouse fixture already covers this (default None), but assert it explicitly once
    here so the "no note" contract is documented alongside the "note present" tests above."""
    player = _make_player(status="a", ep_next="8.0")

    result = project_player_with_champion(None, player, _TARGET_GAMEWEEK_ID, None)

    assert result == project_player(player)
    assert "availability_risk_noted" not in result.source


# --- Role-boost-note stacking (app.optimization.projection's own _apply_role_boost_notes) --
# a separate, independent mechanism from availability-risk notes -- see
# packages.database.models.player_role_note for the full "why two tables, not one" reasoning. ---


def test_role_boost_note_boosts_an_eligible_projection(monkeypatch):
    player = _make_player(status="a", ep_next="8.0")
    monkeypatch.setattr(
        "app.optimization.projection.get_active_role_boost_note",
        lambda *a, **k: _FakeNote(Decimal("1.2")),
    )

    result = project_player_with_champion(None, player, _TARGET_GAMEWEEK_ID, None)

    assert result.projected_points == pytest.approx(8.0 * 1.2)
    assert result.source == "ep_next_role_boosted"
    assert result.is_eligible is True


def test_role_boost_note_never_applies_to_a_hard_excluded_player(monkeypatch):
    """A role boost must not resurrect/relabel an already-ineligible (i/s/u status) projection --
    mirrors the identical availability-risk-note guard above."""
    player = _make_player(status="i", ep_next="99.9")
    monkeypatch.setattr(
        "app.optimization.projection.get_active_role_boost_note",
        lambda *a, **k: _FakeNote(Decimal("1.5")),
    )

    result = project_player_with_champion(None, player, _TARGET_GAMEWEEK_ID, None)

    assert result.source == "unavailable_status_excluded"
    assert result.projected_points == 0.0
    assert result.is_eligible is False


def test_no_active_role_boost_note_leaves_projection_unchanged(monkeypatch):
    """The autouse fixture already covers this (default None), but assert it explicitly once
    here so the "no note" contract is documented alongside the "note present" tests above."""
    player = _make_player(status="a", ep_next="8.0")

    result = project_player_with_champion(None, player, _TARGET_GAMEWEEK_ID, None)

    assert result == project_player(player)
    assert "role_boosted" not in result.source


def test_availability_risk_note_and_role_boost_note_both_apply_when_both_are_active(monkeypatch):
    """The core reason these are two separate mechanisms, not one: a player carrying a minor
    knock (risk discount) who has ALSO just been handed penalty duty (role boost) must get BOTH
    effects compounded, not one silently overriding the other."""
    monkeypatch.setattr(
        "app.optimization.projection.get_active_availability_risk_note",
        lambda *a, **k: _FakeNote(Decimal("0.7"), reason="minor knock"),
    )
    monkeypatch.setattr(
        "app.optimization.projection.get_active_role_boost_note",
        lambda *a, **k: _FakeNote(Decimal("1.3"), reason="now on penalties"),
    )
    player = _make_player(status="a", ep_next="10.0")

    result = project_player_with_champion(None, player, _TARGET_GAMEWEEK_ID, None)

    assert result.projected_points == pytest.approx(10.0 * 0.7 * 1.3)
    assert result.source == "ep_next_availability_risk_noted_role_boosted"
    assert result.is_eligible is True
