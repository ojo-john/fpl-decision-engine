"""Unit tests for app.h2h.strategy (pure, no DB/network) -- Milestone 5 Layer 4.

Mirrors test_h2h_simulate.py's own "hand-built inputs, no DB" discipline.
"""

import uuid

from app.h2h.simulate import UserPlayerInput
from app.h2h.strategy import (
    CandidateResult,
    PickSwap,
    StarterCandidate,
    apply_pick_swaps,
    final_captain_player_id,
    h2h_adjustment_materially_changed,
    recaptain,
    select_captain_cover_captain,
    select_differential_captain,
    select_high_upside_captain,
    select_low_variance_captain,
    select_recommended_candidate,
)
from packages.domain.h2h import H2HStrategyLabel

_P1 = uuid.UUID("00000000-0000-0000-0000-000000000001")
_P2 = uuid.UUID("00000000-0000-0000-0000-000000000002")
_P3 = uuid.UUID("00000000-0000-0000-0000-000000000003")


def _starter(player_id: uuid.UUID = _P1, **overrides) -> StarterCandidate:
    defaults = dict(
        player_id=player_id,
        projected_points=5.0,
        own_residuals=(-1.0, 0.0, 1.0),
        position_residuals=(),
        chance_of_playing_next_round=None,
        selected_by_percent=None,
    )
    defaults.update(overrides)
    return StarterCandidate(**defaults)


def test_select_low_variance_captain_prefers_reliable_over_swingy_equal_mean():
    # Both project 10.0. p1 has a wide residual spread (high std -- penalized);
    # p2 has zero spread (fully reliable) -- low-variance must pick p2.
    starters = [
        _starter(_P1, projected_points=10.0, own_residuals=(-10.0, 0.0, 10.0, -10.0, 0.0, 10.0)),
        _starter(_P2, projected_points=10.0, own_residuals=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)),
    ]

    assert select_low_variance_captain(starters) == _P2


def test_select_high_upside_captain_prefers_swingy_ceiling_over_reliable_equal_mean():
    starters = [
        _starter(_P1, projected_points=10.0, own_residuals=(-10.0, 0.0, 10.0, -10.0, 0.0, 10.0)),
        _starter(_P2, projected_points=10.0, own_residuals=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)),
    ]

    assert select_high_upside_captain(starters) == _P1


def test_select_low_variance_and_high_upside_are_deterministic_on_a_true_tie():
    # Identical projected_points AND identical residuals -- score is exactly tied both ways;
    # the lowest player_id string wins (documented tie-break).
    starters = [_starter(_P2, projected_points=8.0), _starter(_P1, projected_points=8.0)]

    assert select_low_variance_captain(starters) == _P1
    assert select_high_upside_captain(starters) == _P1


def test_select_captain_cover_captain_picks_best_projection_among_top_2_floor():
    # p1: highest projection but a real doubt (60% -- floor risk). p2/p3: fully nailed on (no
    # doubt recorded), p3 projects higher than p2. Top-2 floor = {p2, p3} (both chance=None ->
    # treated as 100, ahead of p1's 60) -- of those two, p3 has the higher projection.
    starters = [
        _starter(_P1, projected_points=12.0, chance_of_playing_next_round=60),
        _starter(_P2, projected_points=7.0, chance_of_playing_next_round=None),
        _starter(_P3, projected_points=9.0, chance_of_playing_next_round=None),
    ]

    assert select_captain_cover_captain(starters) == _P3


def test_select_captain_cover_captain_keeps_single_highest_projected_when_also_highest_floor():
    starters = [
        _starter(_P1, projected_points=12.0, chance_of_playing_next_round=100),
        _starter(_P2, projected_points=7.0, chance_of_playing_next_round=100),
        _starter(_P3, projected_points=9.0, chance_of_playing_next_round=40),
    ]

    assert select_captain_cover_captain(starters) == _P1


def test_select_differential_captain_picks_highest_ceiling_among_lower_owned_half():
    # p1: heavily owned (60%), highest raw projection. p2/p3: lower-owned (below the
    # 3-way median of 60/10/5 = 10 -- only p3 at 5% strictly qualifies). p3's ceiling wins by
    # construction (only qualifying candidate).
    starters = [
        _starter(_P1, projected_points=15.0, selected_by_percent=60.0),
        _starter(_P2, projected_points=8.0, selected_by_percent=10.0),
        _starter(_P3, projected_points=7.0, selected_by_percent=5.0),
    ]

    assert select_differential_captain(starters) == _P3


def test_select_differential_captain_falls_back_to_full_pool_when_none_qualifies():
    # All 3 tied on ownership -- nobody is STRICTLY below the median, so the fallback is the
    # full starter list; highest projection (also highest ceiling, zero residual spread) wins.
    starters = [
        _starter(_P1, projected_points=5.0, selected_by_percent=20.0, own_residuals=()),
        _starter(_P2, projected_points=9.0, selected_by_percent=20.0, own_residuals=()),
        _starter(_P3, projected_points=3.0, selected_by_percent=20.0, own_residuals=()),
    ]

    assert select_differential_captain(starters) == _P2


def test_select_differential_captain_treats_missing_ownership_as_zero():
    starters = [
        _starter(_P1, projected_points=15.0, selected_by_percent=60.0),
        _starter(_P2, projected_points=8.0, selected_by_percent=None),
    ]

    assert select_differential_captain(starters) == _P2


def test_apply_pick_swaps_replaces_element_preserving_slot_fields():
    picks = [
        {"element": 100, "position": 1, "multiplier": 2, "is_captain": True, "is_vice_captain": False},
        {"element": 200, "position": 2, "multiplier": 1, "is_captain": False, "is_vice_captain": True},
    ]
    swaps = [PickSwap(player_out_element_id=100, player_in_element_id=999)]

    result = apply_pick_swaps(picks, swaps)

    assert result[0] == {
        "element": 999,
        "position": 1,
        "multiplier": 2,
        "is_captain": True,
        "is_vice_captain": False,
    }
    assert result[1] == picks[1]
    # Never mutates the input.
    assert picks[0]["element"] == 100


def test_apply_pick_swaps_returns_new_list_unchanged_when_no_swaps_apply():
    picks = [{"element": 100, "position": 1, "multiplier": 1, "is_captain": False, "is_vice_captain": False}]

    result = apply_pick_swaps(picks, [])

    assert result == picks
    assert result is not picks


def test_recaptain_moves_the_armband_multiplier_to_the_new_captain():
    user_players = (
        UserPlayerInput(player_id="a", projected_points=10.0, multiplier=2),
        UserPlayerInput(player_id="b", projected_points=5.0, multiplier=1),
    )

    result = recaptain(user_players, "b")

    by_id = {p.player_id: p.multiplier for p in result}
    assert by_id == {"a": 1, "b": 2}


def test_recaptain_preserves_a_triple_captain_chip_multiplier():
    user_players = (
        UserPlayerInput(player_id="a", projected_points=10.0, multiplier=3),
        UserPlayerInput(player_id="b", projected_points=5.0, multiplier=1),
    )

    result = recaptain(user_players, "b")

    by_id = {p.player_id: p.multiplier for p in result}
    assert by_id == {"a": 1, "b": 3}


def test_recaptain_is_a_no_op_when_the_armband_was_already_lost():
    user_players = (
        UserPlayerInput(player_id="a", projected_points=10.0, multiplier=1),
        UserPlayerInput(player_id="b", projected_points=5.0, multiplier=1),
    )

    result = recaptain(user_players, "b")

    assert result == user_players


def test_final_captain_player_id_returns_none_when_no_armband():
    user_players = (
        UserPlayerInput(player_id="a", projected_points=10.0, multiplier=1),
        UserPlayerInput(player_id="b", projected_points=5.0, multiplier=1),
    )

    assert final_captain_player_id(user_players) is None


def test_final_captain_player_id_returns_the_multiplied_player():
    user_players = (
        UserPlayerInput(player_id="a", projected_points=10.0, multiplier=1),
        UserPlayerInput(player_id="b", projected_points=5.0, multiplier=2),
    )

    assert final_captain_player_id(user_players) == "b"


def _candidate(label: H2HStrategyLabel, win_probability: float) -> CandidateResult:
    return CandidateResult(
        strategy_label=label,
        win_probability=win_probability,
        draw_probability=0.1,
        loss_probability=1.0 - win_probability - 0.1,
        expected_margin=1.0,
    )


def test_select_recommended_candidate_picks_strictly_highest_win_probability():
    candidates = [
        _candidate(H2HStrategyLabel.EP_OPTIMAL, 0.50),
        _candidate(H2HStrategyLabel.CAPTAIN_COVER, 0.65),
        _candidate(H2HStrategyLabel.NO_HIT, 0.50),
    ]

    recommended = select_recommended_candidate(candidates)

    assert recommended.strategy_label == H2HStrategyLabel.CAPTAIN_COVER


def test_select_recommended_candidate_prefers_ep_optimal_on_a_tie():
    candidates = [
        _candidate(H2HStrategyLabel.NO_HIT, 0.55),
        _candidate(H2HStrategyLabel.EP_OPTIMAL, 0.55),
        _candidate(H2HStrategyLabel.CAPTAIN_COVER, 0.55),
    ]

    recommended = select_recommended_candidate(candidates)

    assert recommended.strategy_label == H2HStrategyLabel.EP_OPTIMAL


def test_select_recommended_candidate_prefers_no_transfer_over_transfer_option_on_a_tie():
    candidates = [
        _candidate(H2HStrategyLabel.CALCULATED_HIT, 0.60),
        _candidate(H2HStrategyLabel.HIGH_UPSIDE, 0.60),
    ]

    recommended = select_recommended_candidate(candidates)

    assert recommended.strategy_label == H2HStrategyLabel.HIGH_UPSIDE


def test_select_recommended_candidate_falls_back_to_declaration_order_as_final_tie_break():
    # LOW_VARIANCE declared before HIGH_UPSIDE in H2HStrategyLabel -- both no-transfer, exact tie.
    candidates = [
        _candidate(H2HStrategyLabel.HIGH_UPSIDE, 0.60),
        _candidate(H2HStrategyLabel.LOW_VARIANCE, 0.60),
    ]

    recommended = select_recommended_candidate(candidates)

    assert recommended.strategy_label == H2HStrategyLabel.LOW_VARIANCE


def test_h2h_adjustment_materially_changed_true_when_recommended_is_not_ep_optimal():
    recommended = _candidate(H2HStrategyLabel.DIFFERENTIAL_CAPTAIN, 0.70)

    assert h2h_adjustment_materially_changed(recommended) is True


def test_h2h_adjustment_materially_changed_false_when_recommended_is_ep_optimal():
    recommended = _candidate(H2HStrategyLabel.EP_OPTIMAL, 0.55)

    assert h2h_adjustment_materially_changed(recommended) is False
