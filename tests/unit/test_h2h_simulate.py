"""Unit tests for app.h2h.simulate.simulate_matchup (pure, no DB/network).

Every test injects a seeded ``random.Random`` per the module's own
"never touch the global random module" contract, so results are exactly
reproducible. Test squads are hand-built (2-4 players, not full 11-a-side)
since the simulation logic itself is position/count-agnostic -- what matters
is the residual/transfer/captain machinery, not squad size.
"""

import random

from app.h2h.simulate import (
    RivalHeldPlayerInput,
    RivalTransferTargetInput,
    SimulationInputs,
    UserPlayerInput,
    apply_hit_cost_penalty,
    simulate_matchup,
)
from packages.domain.h2h import RivalStateConfidence


def _user_player(**overrides) -> UserPlayerInput:
    defaults = dict(
        player_id="user-1",
        projected_points=5.0,
        multiplier=1,
        own_residuals=(-1.0, 0.0, 1.0),
        position_residuals=(),
    )
    defaults.update(overrides)
    return UserPlayerInput(**defaults)


def _rival_held(**overrides) -> RivalHeldPlayerInput:
    defaults = dict(
        player_id="rival-1",
        fpl_element_id=101,
        position="MID",
        projected_points=5.0,
        own_residuals=(-1.0, 0.0, 1.0),
        position_residuals=(),
    )
    defaults.update(overrides)
    return RivalHeldPlayerInput(**defaults)


def _rival_target(**overrides) -> RivalTransferTargetInput:
    defaults = dict(
        player_id="target-1",
        position="MID",
        projected_points=20.0,
        own_residuals=(-1.0, 0.0, 1.0),
        position_residuals=(),
    )
    defaults.update(overrides)
    return RivalTransferTargetInput(**defaults)


def test_dominant_user_squad_wins_near_all_draws():
    user_players = tuple(_user_player(player_id=f"u{i}", projected_points=10.0) for i in range(5))
    rival_players = tuple(_rival_held(player_id=f"r{i}", projected_points=2.0) for i in range(5))
    inputs = SimulationInputs(
        user_players=user_players,
        rival_held_players=rival_players,
        rival_state_confidence=RivalStateConfidence.MEDIUM,
        sample_count=5000,
    )

    result = simulate_matchup(inputs, random.Random(42))

    assert result.win_probability > 0.99
    assert result.loss_probability < 0.01
    assert result.expected_margin > 0


def test_near_identical_squads_land_near_fifty_fifty_with_a_low_draw_rate():
    # One user player is given the real squad's captain multiplier (2x); the
    # rival's captain is pinned to its equivalent counterpart via an explicit
    # predicted_captain_probabilities=1.0 (rather than left to the empty-dict
    # "best-per-draw" fallback, which would pick whichever of the 5
    # identically-projected rivals drew the best residual noise that draw --
    # a systematically stronger captain than the user's FIXED captain, which
    # would bias this "near-identical" comparison).
    user_players = (_user_player(player_id="u0", projected_points=5.0, multiplier=2),) + tuple(
        _user_player(player_id=f"u{i}", projected_points=5.0) for i in range(1, 5)
    )
    rival_players = tuple(
        _rival_held(player_id=f"r{i}", fpl_element_id=100 + i, projected_points=5.0) for i in range(5)
    )
    inputs = SimulationInputs(
        user_players=user_players,
        rival_held_players=rival_players,
        rival_captain_probabilities={"100": 1.0},
        rival_state_confidence=RivalStateConfidence.MEDIUM,
        sample_count=5000,
    )

    result = simulate_matchup(inputs, random.Random(7))

    assert 0.35 <= result.win_probability <= 0.65
    assert 0.35 <= result.loss_probability <= 0.65
    assert 0.0 < result.draw_probability < 0.3
    assert result.win_probability + result.draw_probability + result.loss_probability == 1.0


def test_confidence_caps_to_low_when_rival_profile_confidence_is_low_regardless_of_win_probability():
    user_players = tuple(_user_player(player_id=f"u{i}", projected_points=10.0) for i in range(5))
    rival_players = tuple(_rival_held(player_id=f"r{i}", projected_points=2.0) for i in range(5))
    inputs = SimulationInputs(
        user_players=user_players,
        rival_held_players=rival_players,
        rival_state_confidence=RivalStateConfidence.LOW,
        sample_count=1000,
    )

    result = simulate_matchup(inputs, random.Random(1))

    assert result.win_probability > 0.99  # a tight, seemingly-confident result...
    assert result.confidence == RivalStateConfidence.LOW  # ...must never imply more certainty than this


def test_rival_transfer_sampling_varies_the_simulated_squad_across_draws_when_multi_transfer_likely():
    # Deterministic per-draw scores (empty residual pools -> zero noise) isolates
    # the transfer-sampling machinery as the ONLY source of across-draw variation.
    held = tuple(
        _rival_held(player_id=f"r{i}", fpl_element_id=100 + i, projected_points=5.0) for i in range(4)
    )
    targets = (
        _rival_target(player_id="target-a", projected_points=15.0),
        _rival_target(player_id="target-b", projected_points=25.0),
    )
    inputs = SimulationInputs(
        user_players=tuple(_user_player(player_id=f"u{i}") for i in range(4)),
        rival_held_players=held,
        rival_transfer_targets=targets,
        rival_no_transfer_probability=0.0,
        rival_one_transfer_probability=0.0,
        rival_multi_transfer_probability=1.0,
        rival_state_confidence=RivalStateConfidence.MEDIUM,
        sample_count=200,
    )

    result = simulate_matchup(inputs, random.Random(3))

    # Held-squad-only baseline (no transfer) would always sum to 5*4=20 (doubled
    # captain aside) -- since multi_transfer_probability=1.0 forces a swap every
    # draw and the two candidates have very different point values, the rival's
    # simulated mean must sit well above the untouched-squad baseline.
    assert result.rival_score_mean > 20.0


def test_multi_transfer_swaps_two_distinct_weak_positions_not_the_same_one_twice():
    # 2 DEF + 2 MID held, all zero-residual (deterministic). Targets carry two
    # DISTINCT positions (DEF first -- the weakest tracked -- then MID), each
    # with only one candidate and a different point value, so the outcome is
    # fully deterministic and unambiguously proves BOTH positions got swapped
    # independently (a bug that swapped the same position twice, or ignored
    # position entirely, could not land on exactly this total).
    held = (
        _rival_held(player_id="d0", fpl_element_id=1, position="DEF", projected_points=5.0, own_residuals=()),
        _rival_held(player_id="d1", fpl_element_id=2, position="DEF", projected_points=5.0, own_residuals=()),
        _rival_held(player_id="m0", fpl_element_id=3, position="MID", projected_points=5.0, own_residuals=()),
        _rival_held(player_id="m1", fpl_element_id=4, position="MID", projected_points=5.0, own_residuals=()),
    )
    targets = (
        _rival_target(player_id="def-target", position="DEF", projected_points=20.0, own_residuals=()),
        _rival_target(player_id="mid-target", position="MID", projected_points=30.0, own_residuals=()),
    )
    inputs = SimulationInputs(
        user_players=(_user_player(),),
        rival_held_players=held,
        rival_transfer_targets=targets,
        rival_no_transfer_probability=0.0,
        rival_one_transfer_probability=0.0,
        rival_multi_transfer_probability=1.0,
        rival_state_confidence=RivalStateConfidence.MEDIUM,
        sample_count=25,
    )

    result = simulate_matchup(inputs, random.Random(17))

    # Untouched DEF (5) + untouched MID (5) + swapped DEF (20) + swapped MID (30, doubled by the
    # cold-start captain fallback since it's the highest scorer every draw) = 5+5+20+60 = 90,
    # exactly every draw -- deterministic since each position group has one candidate and zero
    # residual noise. A bug that swapped the same position twice (or ignored position entirely)
    # could not land on exactly this total.
    assert result.rival_score_mean == 90.0


def test_cold_start_captain_fallback_captains_best_projected_player_without_crashing():
    # Zero residual noise makes each draw's per-player score exactly its
    # projection, so the fallback captain choice (highest score THAT draw) is
    # fully deterministic and independently verifiable.
    held = (
        _rival_held(
            player_id="r0", fpl_element_id=201, projected_points=10.0, own_residuals=(), position_residuals=()
        ),
        _rival_held(
            player_id="r1", fpl_element_id=202, projected_points=20.0, own_residuals=(), position_residuals=()
        ),
        _rival_held(
            player_id="r2", fpl_element_id=203, projected_points=15.0, own_residuals=(), position_residuals=()
        ),
    )
    inputs = SimulationInputs(
        user_players=(_user_player(),),
        rival_held_players=held,
        rival_transfer_targets=(),
        rival_captain_probabilities={},  # empty: realistic cold-start state
        rival_state_confidence=RivalStateConfidence.LOW,
        sample_count=1,
    )

    result = simulate_matchup(inputs, random.Random(5))

    # 10 + 20*2 + 15 = 65: player r1 (highest-projected) is captained every draw.
    assert result.rival_score_mean == 65.0


def test_residual_bootstrap_cold_start_fallback_degrades_gracefully_with_no_history():
    # Both own_residuals and position_residuals empty -- the honest final
    # fallback (zero noise, pure projection) must never raise.
    user_players = (_user_player(projected_points=7.0, own_residuals=(), position_residuals=()),)
    rival_players = (_rival_held(projected_points=4.0, own_residuals=(), position_residuals=()),)
    inputs = SimulationInputs(
        user_players=user_players,
        rival_held_players=rival_players,
        rival_state_confidence=RivalStateConfidence.LOW,
        sample_count=10,
    )

    result = simulate_matchup(inputs, random.Random(11))

    assert result.user_score_mean == 7.0
    # A single-player rival squad is trivially its own highest scorer, so the
    # cold-start captain fallback still doubles it (2 * 4.0 = 8.0).
    assert result.rival_score_mean == 8.0


def test_user_captain_multiplier_applied_deterministically():
    user_players = (
        _user_player(
            player_id="u0", projected_points=10.0, multiplier=2, own_residuals=(), position_residuals=()
        ),
        _user_player(
            player_id="u1", projected_points=5.0, multiplier=1, own_residuals=(), position_residuals=()
        ),
    )
    rival_players = (_rival_held(projected_points=0.0, own_residuals=(), position_residuals=()),)
    inputs = SimulationInputs(
        user_players=user_players,
        rival_held_players=rival_players,
        rival_state_confidence=RivalStateConfidence.MEDIUM,
        sample_count=1,
    )

    result = simulate_matchup(inputs, random.Random(2))

    assert result.user_score_mean == 25.0  # 10*2 (captain) + 5


def test_sample_count_is_respected():
    inputs = SimulationInputs(
        user_players=(_user_player(),),
        rival_held_players=(_rival_held(),),
        rival_state_confidence=RivalStateConfidence.MEDIUM,
        sample_count=123,
    )

    result = simulate_matchup(inputs, random.Random(9))

    assert result.sample_count == 123


# --- apply_hit_cost_penalty ------------------------------------------------------------------
# Real bug this closes (found and fixed 2026-09-07): nothing in the H2H simulation path ever
# subtracted a real transfer-hit point penalty -- a CALCULATED_HIT candidate whose recommended
# scenario genuinely took a hit had its win probability computed against the full, un-penalized
# squad score, silently overstating it by exactly hit_cost points.


def test_apply_hit_cost_penalty_zero_hit_cost_returns_scores_unchanged():
    scores = (40, 55, 62)

    assert apply_hit_cost_penalty(scores, 0) == scores


def test_apply_hit_cost_penalty_subtracts_hit_cost_from_every_sample():
    scores = (40, 55, 62)

    assert apply_hit_cost_penalty(scores, 4) == (36, 51, 58)


def test_apply_hit_cost_penalty_can_take_a_score_negative():
    # A real, if extreme, possibility -- a poor sampled draw combined with a hit large enough
    # (e.g. two paid transfers, -8) can genuinely leave a negative effective score; this must
    # never be clamped to zero, since that would understate how bad a real double-hit can be.
    scores = (2,)

    assert apply_hit_cost_penalty(scores, 8) == (-6,)


def test_apply_hit_cost_penalty_empty_scores_returns_empty():
    assert apply_hit_cost_penalty((), 4) == ()
