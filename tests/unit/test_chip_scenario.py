"""Unit tests for app.chips.scenario (pure Bench Boost/Triple Captain/ranking logic, no DB/network).

Wildcard/Free Hit need a real DB session for their MIP rebuild -- see
tests/integration/test_chip_scenario.py instead (mirrors app.h2h.simulate's
own unit-vs-integration split: everything hand-buildable with plain Python
objects lives here; everything needing a live solver/session lives there).

Every Monte Carlo test injects a seeded random.Random per this codebase's own
"never touch the global random module" contract (see app.h2h.simulate's own
module docstring), so results are exactly reproducible.
"""

import random
import uuid

from app.chips.scenario import (
    BenchBoostGameweekCandidate,
    BenchPlayerInput,
    ChipWindowCandidateResult,
    TripleCaptainGameweekCandidate,
    _rebuild_budget,
    evaluate_bench_boost_candidate,
    evaluate_triple_captain_candidate,
    rank_candidates_for_chip,
    top_captain_candidates,
)
from app.h2h.simulate import UserPlayerInput
from app.optimization.fixture_calendar import TeamFixtureCount
from packages.domain.chips import ChipVerdict

TEAM_A = uuid.uuid4()
TEAM_B = uuid.uuid4()
TEAM_C = uuid.uuid4()
TEAM_D = uuid.uuid4()

_GAMEWEEK_ID = uuid.uuid4()


def _starter(**overrides) -> UserPlayerInput:
    defaults = dict(
        player_id="starter-1",
        projected_points=5.0,
        multiplier=1,
        own_residuals=(),
        position_residuals=(),
    )
    defaults.update(overrides)
    return UserPlayerInput(**defaults)


def _bench(**overrides) -> BenchPlayerInput:
    defaults = dict(
        player_id="bench-1",
        team_id=TEAM_A,
        projected_points=4.0,
        own_residuals=(),
        position_residuals=(),
    )
    defaults.update(overrides)
    return BenchPlayerInput(**defaults)


def _fixture_count(team_id: uuid.UUID, count: int) -> TeamFixtureCount:
    return TeamFixtureCount(team_id=team_id, fixture_count=count, fixture_ids=())


# --- Bench Boost --------------------------------------------------------------------------------


def test_bench_boost_gain_equals_sum_of_bench_players_at_normal_fixture_count():
    # Zero residual noise (deterministic draws == projections) isolates the gain calculation itself.
    starters = tuple(_starter(player_id=f"s{i}", projected_points=6.0) for i in range(11))
    bench = (
        _bench(player_id="b0", team_id=TEAM_A, projected_points=3.0),
        _bench(player_id="b1", team_id=TEAM_B, projected_points=5.0),
        _bench(player_id="b2", team_id=TEAM_C, projected_points=1.0),
        _bench(player_id="b3", team_id=TEAM_D, projected_points=2.0),
    )
    candidate = BenchBoostGameweekCandidate(
        fpl_event_id=10,
        gameweek_id=_GAMEWEEK_ID,
        starters=starters,
        bench=bench,
        team_fixture_counts={
            TEAM_A: _fixture_count(TEAM_A, 1),
            TEAM_B: _fixture_count(TEAM_B, 1),
            TEAM_C: _fixture_count(TEAM_C, 1),
            TEAM_D: _fixture_count(TEAM_D, 1),
        },
    )

    result = evaluate_bench_boost_candidate(candidate, random.Random(1), sample_count=50)

    # 3 + 5 + 1 + 2 = 11, every draw (zero residual noise -> deterministic).
    assert result.gain_mean == 11.0
    assert result.gain_floor == 11.0
    assert result.gain_ceiling == 11.0
    assert result.fpl_event_id == 10
    assert result.gameweek_id == _GAMEWEEK_ID


def test_bench_boost_fixture_count_multiplier_doubles_a_double_gameweek_bench_players_contribution():
    starters = tuple(_starter(player_id=f"s{i}", projected_points=6.0) for i in range(11))
    bench = (
        _bench(player_id="b0", team_id=TEAM_A, projected_points=3.0),  # normal (1 fixture)
        _bench(player_id="b1", team_id=TEAM_B, projected_points=5.0),  # DOUBLE gameweek (2 fixtures)
        _bench(player_id="b2", team_id=TEAM_C, projected_points=1.0),  # BLANK gameweek (0 fixtures)
        _bench(player_id="b3", team_id=TEAM_D, projected_points=2.0),  # normal
    )
    candidate = BenchBoostGameweekCandidate(
        fpl_event_id=11,
        gameweek_id=_GAMEWEEK_ID,
        starters=starters,
        bench=bench,
        team_fixture_counts={
            TEAM_A: _fixture_count(TEAM_A, 1),
            TEAM_B: _fixture_count(TEAM_B, 2),
            TEAM_C: _fixture_count(TEAM_C, 0),
            TEAM_D: _fixture_count(TEAM_D, 1),
        },
    )

    result = evaluate_bench_boost_candidate(candidate, random.Random(2), sample_count=50)

    # 3*1 + 5*2 + 1*0 + 2*1 = 3 + 10 + 0 + 2 = 15.
    assert result.gain_mean == 15.0


def test_bench_boost_fixture_count_multiplier_only_looks_at_bench_players_own_teams():
    # Changing an UNRELATED team's fixture count (a team with no bench player at all -- note
    # UserPlayerInput/starters have no team_id concept in this data model, so a starter's own team
    # can never be looked up here in the first place) must never affect the gain calculation --
    # proves the multiplier is scoped strictly to exactly the bench players' own teams.
    bench = (
        _bench(player_id="b0", team_id=TEAM_B, projected_points=3.0),
        _bench(player_id="b1", team_id=TEAM_C, projected_points=3.0),
        _bench(player_id="b2", team_id=TEAM_C, projected_points=3.0),
        _bench(player_id="b3", team_id=TEAM_C, projected_points=3.0),
    )
    starters = tuple(_starter(player_id=f"s{i}", projected_points=6.0) for i in range(11))

    base_counts = {TEAM_B: _fixture_count(TEAM_B, 1), TEAM_C: _fixture_count(TEAM_C, 1)}
    counts_with_unrelated_team_doubled = {**base_counts, TEAM_A: _fixture_count(TEAM_A, 2)}

    candidate_base = BenchBoostGameweekCandidate(
        fpl_event_id=12,
        gameweek_id=_GAMEWEEK_ID,
        starters=starters,
        bench=bench,
        team_fixture_counts=base_counts,
    )
    candidate_with_unrelated_team = BenchBoostGameweekCandidate(
        fpl_event_id=12,
        gameweek_id=_GAMEWEEK_ID,
        starters=starters,
        bench=bench,
        team_fixture_counts=counts_with_unrelated_team_doubled,
    )

    result_base = evaluate_bench_boost_candidate(candidate_base, random.Random(3), sample_count=50)
    result_with_unrelated_team = evaluate_bench_boost_candidate(
        candidate_with_unrelated_team, random.Random(3), sample_count=50
    )

    assert result_base.gain_mean == result_with_unrelated_team.gain_mean == 12.0  # 3+3+3+3 @ multiplier 1


def test_bench_boost_paired_draws_are_bit_identical_across_reruns_with_the_same_seed():
    starters = tuple(
        _starter(player_id=f"s{i}", projected_points=5.0, own_residuals=(-2.0, 0.0, 2.0)) for i in range(11)
    )
    bench = tuple(
        _bench(player_id=f"b{i}", team_id=TEAM_A, projected_points=4.0, own_residuals=(-1.0, 0.0, 1.0))
        for i in range(4)
    )
    candidate = BenchBoostGameweekCandidate(
        fpl_event_id=13,
        gameweek_id=_GAMEWEEK_ID,
        starters=starters,
        bench=bench,
        team_fixture_counts={TEAM_A: _fixture_count(TEAM_A, 1)},
    )

    result_a = evaluate_bench_boost_candidate(candidate, random.Random(99), sample_count=500)
    result_b = evaluate_bench_boost_candidate(candidate, random.Random(99), sample_count=500)

    assert result_a == result_b


# --- Triple Captain ------------------------------------------------------------------------------


def test_triple_captain_incremental_gain_is_captains_own_points_not_full_3x_total():
    # Zero residual noise -> deterministic draws == projections, isolating the formula itself.
    player = _starter(player_id="c0", projected_points=8.0)
    candidate = TripleCaptainGameweekCandidate(
        fpl_event_id=20, gameweek_id=_GAMEWEEK_ID, captain_pool=(player,)
    )

    result = evaluate_triple_captain_candidate(
        candidate, captain_multiplier=2, rng=random.Random(4), sample_count=50
    )

    # Incremental gain = captain's own points once more (3x - 2x = 1x = 8.0), NOT the full 3x=24.0 total.
    assert result.gain_mean == 8.0
    assert result.gain_floor == 8.0
    assert result.gain_ceiling == 8.0


def test_triple_captain_best_candidate_is_correctly_identified_among_several():
    low = _starter(player_id="low", projected_points=4.0)
    high = _starter(player_id="high", projected_points=10.0)
    mid = _starter(player_id="mid", projected_points=7.0)
    candidate = TripleCaptainGameweekCandidate(
        fpl_event_id=21, gameweek_id=_GAMEWEEK_ID, captain_pool=(low, high, mid)
    )

    result = evaluate_triple_captain_candidate(
        candidate, captain_multiplier=2, rng=random.Random(5), sample_count=50
    )

    # The highest-projected candidate ("high", 10.0 points) must yield the highest incremental gain
    # when every candidate has zero residual noise (no risk-adjustment reason to prefer another).
    assert result.gain_mean == 10.0


def test_top_captain_candidates_returns_highest_projected_starters_first():
    starters = tuple(_starter(player_id=f"s{i}", projected_points=float(i)) for i in range(6))

    pool = top_captain_candidates(starters, pool_size=3)

    assert [p.player_id for p in pool] == ["s5", "s4", "s3"]


def test_triple_captain_paired_draws_are_bit_identical_across_reruns_with_the_same_seed():
    player = _starter(player_id="c0", projected_points=9.0, own_residuals=(-3.0, 0.0, 3.0))
    candidate = TripleCaptainGameweekCandidate(
        fpl_event_id=22, gameweek_id=_GAMEWEEK_ID, captain_pool=(player,)
    )

    result_a = evaluate_triple_captain_candidate(
        candidate, captain_multiplier=2, rng=random.Random(77), sample_count=500
    )
    result_b = evaluate_triple_captain_candidate(
        candidate, captain_multiplier=2, rng=random.Random(77), sample_count=500
    )

    assert result_a == result_b


# --- rank_candidates_for_chip ----------------------------------------------------------------


def _result(fpl_event_id: int, gain_mean: float) -> ChipWindowCandidateResult:
    return ChipWindowCandidateResult(
        fpl_event_id=fpl_event_id,
        gameweek_id=uuid.uuid4(),
        gain_mean=gain_mean,
        gain_floor=gain_mean,
        gain_ceiling=gain_mean,
    )


def test_rank_candidates_use_now_when_immediate_is_already_best():
    candidates = [_result(10, 8.0), _result(11, 3.0), _result(12, 1.0)]

    recommendation = rank_candidates_for_chip(candidates)

    assert recommendation.verdict == ChipVerdict.USE_NOW
    assert recommendation.recommended_fpl_event_id == 10
    assert recommendation.recommended_gain_mean == 8.0
    assert "GW10" in recommendation.rationale


def test_rank_candidates_hold_when_a_later_window_clearly_beats_using_it_now():
    candidates = [_result(10, 2.0), _result(11, 3.0), _result(15, 20.0)]

    recommendation = rank_candidates_for_chip(candidates)

    assert recommendation.verdict == ChipVerdict.HOLD
    assert recommendation.recommended_fpl_event_id == 15
    assert recommendation.recommended_gain_mean == 20.0
    # Rationale must be a real, specific sentence naming the gameweek and the gain delta.
    assert "GW15" in recommendation.rationale
    assert "GW10" in recommendation.rationale
    assert "18.00" in recommendation.rationale  # 20.0 - 2.0


def test_rank_candidates_hold_when_every_candidates_gain_is_non_positive():
    candidates = [_result(10, -1.0), _result(11, -0.5), _result(12, 0.0)]

    recommendation = rank_candidates_for_chip(candidates)

    assert recommendation.verdict == ChipVerdict.HOLD
    assert recommendation.recommended_fpl_event_id is None
    assert recommendation.recommended_gain_mean is None


def test_rank_candidates_no_viable_window_when_nothing_was_evaluated():
    recommendation = rank_candidates_for_chip([])

    assert recommendation.verdict == ChipVerdict.NO_VIABLE_WINDOW
    assert recommendation.recommended_fpl_event_id is None


def test_rebuild_budget_equals_bank_plus_sum_of_selling_prices():
    player_a, player_b, player_c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    selling_prices = {player_a: 50, player_b: 45, player_c: 100}

    assert _rebuild_budget(bank=17, selling_prices=selling_prices) == 17 + 50 + 45 + 100


def test_rebuild_budget_with_no_selling_prices_is_just_the_bank():
    assert _rebuild_budget(bank=5, selling_prices={}) == 5


def test_rank_candidates_use_now_when_later_gain_is_within_the_materiality_threshold():
    # Later week is technically better (3.0 > 2.0) but by less than the 2.0-point materiality
    # threshold -- USE_NOW, not HOLD, since the gap is immaterial noise.
    candidates = [_result(10, 2.0), _result(14, 3.0)]

    recommendation = rank_candidates_for_chip(candidates)

    assert recommendation.verdict == ChipVerdict.USE_NOW
    assert recommendation.recommended_fpl_event_id == 10
