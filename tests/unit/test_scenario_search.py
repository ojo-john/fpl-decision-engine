"""Unit tests for app.transfers.scenario_search.evaluate_transfer_scenarios (no DB).

Every input is a plain, already-built TransferCandidate -- these tests never
touch a session, mirroring tests/unit/test_joint_squad_solver.py's toy-pool
style for solver-internals tests.
"""

import uuid

import pytest

from app.transfers.candidates import TransferCandidate
from app.transfers.scenario_search import (
    MAX_TRANSFERS_TO_EVALUATE_CEILING,
    InvalidScenarioSearchInputError,
    evaluate_transfer_scenarios,
)

_TEAM_A = uuid.uuid4()
_TEAM_B = uuid.uuid4()
_TEAM_C = uuid.uuid4()
_TEAM_D = uuid.uuid4()
_TEAM_ARS = uuid.uuid4()
_TEAM_CHE = uuid.uuid4()
_TEAM_MC = uuid.uuid4()
_TEAM_LIV = uuid.uuid4()


def _candidate(position: str, team_id: uuid.UUID, cost: int, horizon_points: float) -> TransferCandidate:
    return TransferCandidate(
        player_id=uuid.uuid4(),
        position_short_name=position,
        team_id=team_id,
        now_cost=cost,
        horizon_points=horizon_points,
        is_eligible=True,
    )


# --- 0-transfer baseline -----------------------------------------------------


def test_zero_transfer_always_available_with_no_gain_and_no_hit_cost():
    scenarios, recommended = evaluate_transfer_scenarios(
        squad_candidates=[],
        in_shortlists_by_position={},
        selling_prices={},
        team_limit=3,
        bank=0,
        free_transfers_available=1,
        transfer_hit_cost_magnitude=4,
    )
    assert scenarios[0].transfers_count == 0
    assert scenarios[0].moves == []
    assert scenarios[0].raw_horizon_points_gain == 0.0
    assert scenarios[0].hit_cost == 0
    assert scenarios[0].net_horizon_points_gain == 0.0


# --- 1-transfer: clearly-better replacement exists ---------------------------


def test_one_transfer_selects_the_clearly_better_swap():
    out = _candidate("DEF", _TEAM_A, cost=50, horizon_points=20.0)
    in_best = _candidate("DEF", _TEAM_B, cost=50, horizon_points=30.0)
    in_worse = _candidate("DEF", _TEAM_C, cost=50, horizon_points=25.0)

    scenarios, _ = evaluate_transfer_scenarios(
        squad_candidates=[out],
        in_shortlists_by_position={"DEF": [in_best, in_worse]},
        selling_prices={out.player_id: 50},
        team_limit=3,
        bank=0,
        free_transfers_available=1,
        transfer_hit_cost_magnitude=4,
    )

    scenario1 = scenarios[1]
    assert len(scenario1.moves) == 1
    assert scenario1.moves[0].player_in.player_id == in_best.player_id
    assert scenario1.moves[0].player_out.player_id == out.player_id
    assert scenario1.raw_horizon_points_gain == pytest.approx(10.0)
    assert scenario1.hit_cost == 0  # free_transfers_available=1 covers 1 transfer
    assert scenario1.net_horizon_points_gain == pytest.approx(10.0)


# --- 1-transfer: budget-infeasible best-looking swap correctly excluded -----


def test_one_transfer_excludes_unaffordable_best_looking_swap():
    out = _candidate("DEF", _TEAM_A, cost=50, horizon_points=20.0)
    # Best by points alone, but costs more than selling_price + bank.
    in_unaffordable = _candidate("DEF", _TEAM_B, cost=80, horizon_points=40.0)
    in_affordable = _candidate("DEF", _TEAM_C, cost=50, horizon_points=25.0)

    scenarios, _ = evaluate_transfer_scenarios(
        squad_candidates=[out],
        in_shortlists_by_position={"DEF": [in_unaffordable, in_affordable]},
        selling_prices={out.player_id: 50},
        team_limit=3,
        bank=0,
        free_transfers_available=1,
        transfer_hit_cost_magnitude=4,
    )

    scenario1 = scenarios[1]
    assert len(scenario1.moves) == 1
    assert scenario1.moves[0].player_in.player_id == in_affordable.player_id
    assert scenario1.raw_horizon_points_gain == pytest.approx(5.0)


# --- 1-transfer: team-limit-infeasible swap correctly excluded -------------


def test_one_transfer_excludes_team_limit_breaching_swap():
    # Squad already has 3 players from TEAM_A (at the team_limit) plus the OUT candidate.
    # None of these three share the OUT candidate's DEF position, so the 1-transfer search
    # never actually considers swapping them out -- but every squad member still needs a
    # priced exit, per evaluate_transfer_scenarios' own precondition.
    p1 = _candidate("GKP", _TEAM_A, cost=40, horizon_points=5.0)
    p2 = _candidate("MID", _TEAM_A, cost=40, horizon_points=5.0)
    p3 = _candidate("FWD", _TEAM_A, cost=40, horizon_points=5.0)
    out = _candidate("DEF", _TEAM_B, cost=50, horizon_points=20.0)

    # Best by points, but would push TEAM_A to 4 (over team_limit=3).
    in_bad = _candidate("DEF", _TEAM_A, cost=50, horizon_points=100.0)
    in_good = _candidate("DEF", _TEAM_C, cost=50, horizon_points=30.0)

    scenarios, _ = evaluate_transfer_scenarios(
        squad_candidates=[p1, p2, p3, out],
        in_shortlists_by_position={"DEF": [in_bad, in_good]},
        selling_prices={p1.player_id: 40, p2.player_id: 40, p3.player_id: 40, out.player_id: 50},
        team_limit=3,
        bank=0,
        free_transfers_available=1,
        transfer_hit_cost_magnitude=4,
    )

    scenario1 = scenarios[1]
    assert len(scenario1.moves) == 1
    assert scenario1.moves[0].player_in.player_id == in_good.player_id
    assert scenario1.raw_horizon_points_gain == pytest.approx(10.0)


# --- 2-transfer: joint team-limit violation correctly caught ---------------


def test_two_transfer_excludes_joint_team_limit_violation_but_finds_legal_alternative():
    """2 Man City players already owned; transferring out an Arsenal DEF + a Chelsea MID and
    bringing in 2 MORE Man City players (one DEF, one MID) would push Man City to 4, over a
    team_limit of 3 -- even though EACH swap in isolation (Man City count 2 -> 3) would be legal.
    A legal alternative (bringing in the Man City DEF alongside a Liverpool MID instead) exists
    and has a lower, but legal, points_gain -- it must be the one actually selected.
    """
    mc_owned_1 = _candidate("GKP", _TEAM_MC, cost=45, horizon_points=5.0)
    mc_owned_2 = _candidate("FWD", _TEAM_MC, cost=45, horizon_points=5.0)
    ars_out = _candidate("DEF", _TEAM_ARS, cost=50, horizon_points=10.0)
    che_out = _candidate("MID", _TEAM_CHE, cost=50, horizon_points=10.0)

    mc_in_def = _candidate("DEF", _TEAM_MC, cost=50, horizon_points=30.0)
    mc_in_mid = _candidate("MID", _TEAM_MC, cost=50, horizon_points=30.0)  # would make the illegal combo
    liv_in_mid = _candidate("MID", _TEAM_LIV, cost=50, horizon_points=20.0)  # legal alternative

    scenarios, _ = evaluate_transfer_scenarios(
        squad_candidates=[mc_owned_1, mc_owned_2, ars_out, che_out],
        in_shortlists_by_position={"DEF": [mc_in_def], "MID": [mc_in_mid, liv_in_mid]},
        selling_prices={
            mc_owned_1.player_id: 45,
            mc_owned_2.player_id: 45,
            ars_out.player_id: 50,
            che_out.player_id: 50,
        },
        team_limit=3,
        bank=0,
        free_transfers_available=2,
        transfer_hit_cost_magnitude=4,
    )

    scenario2 = scenarios[2]
    assert len(scenario2.moves) == 2
    selected_in_ids = {move.player_in.player_id for move in scenario2.moves}
    # The illegal (2x Man City) combination must never be selected...
    assert selected_in_ids != {mc_in_def.player_id, mc_in_mid.player_id}
    # ...the legal alternative (Man City DEF + Liverpool MID) must be, instead.
    assert selected_in_ids == {mc_in_def.player_id, liv_in_mid.player_id}
    # Legal alternative's gain: (30 + 20) - (10 + 10) = 30, NOT the illegal combo's 40.
    assert scenario2.raw_horizon_points_gain == pytest.approx(30.0)


# --- 2-transfer: joint budget checked across both moves together ----------


def test_two_transfer_excludes_combo_unaffordable_only_jointly():
    """Each swap is individually affordable using the full bank in isolation, but the two
    together exceed combined selling price + bank (the bank can't be spent twice).
    """
    out1 = _candidate("DEF", _TEAM_A, cost=50, horizon_points=10.0)
    out2 = _candidate("MID", _TEAM_B, cost=50, horizon_points=10.0)
    in1 = _candidate("DEF", _TEAM_C, cost=55, horizon_points=20.0)  # 55 <= 50 + 10 (affordable alone)
    in2 = _candidate("MID", _TEAM_D, cost=60, horizon_points=20.0)  # 60 <= 50 + 10 (affordable alone)
    # Combined: 55 + 60 = 115 > 50 + 50 + 10 = 110 -- jointly infeasible.

    scenarios, _ = evaluate_transfer_scenarios(
        squad_candidates=[out1, out2],
        in_shortlists_by_position={"DEF": [in1], "MID": [in2]},
        selling_prices={out1.player_id: 50, out2.player_id: 50},
        team_limit=5,
        bank=10,
        free_transfers_available=2,
        transfer_hit_cost_magnitude=4,
    )

    scenario2 = scenarios[2]
    assert scenario2.moves == []
    assert scenario2.raw_horizon_points_gain == 0.0


# --- Hit-cost accrual and recommendation selection --------------------------


def test_roll_recommended_when_best_one_transfer_gain_does_not_clear_hit_cost():
    out = _candidate("DEF", _TEAM_A, cost=50, horizon_points=20.0)
    in_candidate = _candidate("DEF", _TEAM_B, cost=50, horizon_points=23.0)  # raw gain = 3

    scenarios, recommended = evaluate_transfer_scenarios(
        squad_candidates=[out],
        in_shortlists_by_position={"DEF": [in_candidate]},
        selling_prices={out.player_id: 50},
        team_limit=3,
        bank=0,
        free_transfers_available=0,
        transfer_hit_cost_magnitude=4,
    )

    assert scenarios[1].raw_horizon_points_gain == pytest.approx(3.0)
    assert scenarios[1].hit_cost == 4
    assert scenarios[1].net_horizon_points_gain == pytest.approx(-1.0)
    assert recommended.transfers_count == 0


def test_one_transfer_recommended_when_gain_clearly_exceeds_hit_cost():
    out = _candidate("DEF", _TEAM_A, cost=50, horizon_points=20.0)
    in_candidate = _candidate("DEF", _TEAM_B, cost=50, horizon_points=35.0)  # raw gain = 15

    scenarios, recommended = evaluate_transfer_scenarios(
        squad_candidates=[out],
        in_shortlists_by_position={"DEF": [in_candidate]},
        selling_prices={out.player_id: 50},
        team_limit=3,
        bank=0,
        free_transfers_available=0,
        transfer_hit_cost_magnitude=4,
    )

    assert scenarios[1].hit_cost == 4
    assert scenarios[1].net_horizon_points_gain == pytest.approx(11.0)
    assert recommended.transfers_count == 1
    assert recommended.moves[0].player_in.player_id == in_candidate.player_id


# --- Tie-break toward fewer transfers ---------------------------------------


def test_tie_break_prefers_one_transfer_over_two_when_net_gain_is_equal():
    out1 = _candidate("DEF", _TEAM_A, cost=50, horizon_points=10.0)
    out2 = _candidate("MID", _TEAM_B, cost=50, horizon_points=10.0)
    # Single DEF swap gains 10; single MID swap gains 0 -- best 1-transfer = 10.
    in_def = _candidate("DEF", _TEAM_C, cost=50, horizon_points=20.0)
    in_mid = _candidate("MID", _TEAM_D, cost=50, horizon_points=10.0)
    # The only possible 2-transfer combo sums to 10 + 0 = 10 -- an exact tie.

    scenarios, recommended = evaluate_transfer_scenarios(
        squad_candidates=[out1, out2],
        in_shortlists_by_position={"DEF": [in_def], "MID": [in_mid]},
        selling_prices={out1.player_id: 50, out2.player_id: 50},
        team_limit=5,
        bank=0,
        free_transfers_available=2,  # both scenario counts incur zero hit cost
        transfer_hit_cost_magnitude=4,
    )

    assert scenarios[1].net_horizon_points_gain == pytest.approx(10.0)
    assert scenarios[2].net_horizon_points_gain == pytest.approx(10.0)
    assert recommended.transfers_count == 1


def test_tie_break_prefers_zero_transfer_over_one_when_net_gain_is_equal():
    out = _candidate("DEF", _TEAM_A, cost=50, horizon_points=20.0)
    # Raw gain of exactly 0 -- net_gain ties with ROLL's 0.0.
    in_candidate = _candidate("DEF", _TEAM_B, cost=50, horizon_points=20.0)

    scenarios, recommended = evaluate_transfer_scenarios(
        squad_candidates=[out],
        in_shortlists_by_position={"DEF": [in_candidate]},
        selling_prices={out.player_id: 50},
        team_limit=3,
        bank=0,
        free_transfers_available=1,
        transfer_hit_cost_magnitude=4,
    )

    assert scenarios[1].net_horizon_points_gain == pytest.approx(0.0)
    assert recommended.transfers_count == 0


# --- With free_transfers_available >= 2: legit 2-transfer scenario wins -----


def test_two_transfer_recommended_when_it_clearly_beats_zero_and_one_with_no_hit_cost():
    out1 = _candidate("DEF", _TEAM_A, cost=50, horizon_points=10.0)
    out2 = _candidate("MID", _TEAM_B, cost=50, horizon_points=10.0)
    # Single DEF swap gains 15; single MID swap gains 15 -- best 1-transfer = 15.
    in_def = _candidate("DEF", _TEAM_C, cost=50, horizon_points=25.0)
    in_mid = _candidate("MID", _TEAM_D, cost=50, horizon_points=25.0)
    # Doing both together gains 15 + 15 = 30 -- clearly better than either alone.

    scenarios, recommended = evaluate_transfer_scenarios(
        squad_candidates=[out1, out2],
        in_shortlists_by_position={"DEF": [in_def], "MID": [in_mid]},
        selling_prices={out1.player_id: 50, out2.player_id: 50},
        team_limit=5,
        bank=0,
        free_transfers_available=2,
        transfer_hit_cost_magnitude=4,
    )

    assert scenarios[1].raw_horizon_points_gain == pytest.approx(15.0)
    assert scenarios[2].raw_horizon_points_gain == pytest.approx(30.0)
    assert scenarios[2].hit_cost == 0
    assert scenarios[2].net_horizon_points_gain == pytest.approx(30.0)
    assert recommended.transfers_count == 2
    selected_in_ids = {move.player_in.player_id for move in recommended.moves}
    assert selected_in_ids == {in_def.player_id, in_mid.player_id}


# --- Input validation: fail fast at the boundary, never a wrong answer ------


def test_negative_transfer_hit_cost_magnitude_is_rejected():
    """Layer 1's TransferRules.transfer_hit_cost is stored SIGNED (e.g. -4) -- if a caller ever
    wires that raw signed value straight through instead of abs()-ing it first, hit costs would
    silently invert into a points bonus. This must be rejected loudly, not silently miscomputed.
    """
    out = _candidate("DEF", _TEAM_A, cost=50, horizon_points=20.0)

    with pytest.raises(InvalidScenarioSearchInputError):
        evaluate_transfer_scenarios(
            squad_candidates=[out],
            in_shortlists_by_position={},
            selling_prices={out.player_id: 50},
            team_limit=3,
            bank=0,
            free_transfers_available=1,
            transfer_hit_cost_magnitude=-4,  # the Layer-1-signed value, passed by mistake
        )


def test_missing_selling_price_for_squad_member_is_rejected():
    """Every squad_candidates player is a potential OUT candidate and needs a priced exit --
    a caller that omits one gets a clear, typed error here instead of an opaque KeyError deep
    inside the search loop.
    """
    out1 = _candidate("DEF", _TEAM_A, cost=50, horizon_points=20.0)
    out2 = _candidate("MID", _TEAM_B, cost=50, horizon_points=20.0)

    with pytest.raises(InvalidScenarioSearchInputError):
        evaluate_transfer_scenarios(
            squad_candidates=[out1, out2],
            in_shortlists_by_position={},
            selling_prices={out1.player_id: 50},  # out2's selling price is missing
            team_limit=3,
            bank=0,
            free_transfers_available=1,
            transfer_hit_cost_magnitude=4,
        )


# --- Real gap closed 2026-10-06: scenarios now extend past 2 transfers -----


def test_scenarios_extend_up_to_free_transfers_available():
    """4 free transfers, 4 independent beneficial same-position swaps -- before this fix,
    scenarios never went past {0, 1, 2} regardless of free_transfers_available; this confirms
    the search now actually reaches scenarios[4] and recommends it.
    """
    squad = [
        _candidate("GKP", _TEAM_A, cost=40, horizon_points=5.0),
        _candidate("DEF", _TEAM_B, cost=40, horizon_points=5.0),
        _candidate("MID", _TEAM_C, cost=40, horizon_points=5.0),
        _candidate("FWD", _TEAM_D, cost=40, horizon_points=5.0),
    ]
    in_shortlists = {
        "GKP": [_candidate("GKP", _TEAM_ARS, cost=40, horizon_points=15.0)],
        "DEF": [_candidate("DEF", _TEAM_CHE, cost=40, horizon_points=15.0)],
        "MID": [_candidate("MID", _TEAM_MC, cost=40, horizon_points=15.0)],
        "FWD": [_candidate("FWD", _TEAM_LIV, cost=40, horizon_points=15.0)],
    }
    selling_prices = {c.player_id: 40 for c in squad}

    scenarios, recommended = evaluate_transfer_scenarios(
        squad_candidates=squad,
        in_shortlists_by_position=in_shortlists,
        selling_prices=selling_prices,
        team_limit=3,
        bank=0,
        free_transfers_available=4,
        transfer_hit_cost_magnitude=4,
    )

    assert set(scenarios.keys()) == {0, 1, 2, 3, 4}
    assert scenarios[4].raw_horizon_points_gain == pytest.approx(40.0)  # 4 swaps x +10 each
    assert scenarios[4].hit_cost == 0
    assert recommended.transfers_count == 4


def test_scenarios_capped_at_ceiling_even_with_more_free_transfers_available():
    """FPL's real free-transfer rollover caps at 5 -- free_transfers_available=10 (unrealistic,
    but a caller input this function has no way to reject) must not extend the search beyond
    MAX_TRANSFERS_TO_EVALUATE_CEILING.
    """
    out = _candidate("DEF", _TEAM_A, cost=50, horizon_points=20.0)
    scenarios, _ = evaluate_transfer_scenarios(
        squad_candidates=[out],
        in_shortlists_by_position={},
        selling_prices={out.player_id: 50},
        team_limit=3,
        bank=0,
        free_transfers_available=10,
        transfer_hit_cost_magnitude=4,
    )
    assert max(scenarios.keys()) == MAX_TRANSFERS_TO_EVALUATE_CEILING


def test_k_transfer_preserves_position_shape_even_when_an_illegal_combo_scores_higher():
    """Squad has 2 DEF + 1 MID. Two elite MID candidates exist but only one mediocre DEF
    candidate -- an unconstrained solver would happily drop BOTH defenders for the two elite
    MIDs (raw gain 980), ending at 0 DEF / 3 MID, which breaks the squad's real DEF=2/MID=1
    shape. The legal best (drop one DEF + the MID, bring in the one DEF candidate plus one
    elite MID, raw gain 520) must be what's actually selected.
    """
    def_a = _candidate("DEF", _TEAM_A, cost=50, horizon_points=10.0)
    def_b = _candidate("DEF", _TEAM_B, cost=50, horizon_points=10.0)
    mid_c = _candidate("MID", _TEAM_C, cost=50, horizon_points=10.0)

    def_med = _candidate("DEF", _TEAM_D, cost=50, horizon_points=40.0)
    mid_great_1 = _candidate("MID", _TEAM_A, cost=50, horizon_points=500.0)
    mid_great_2 = _candidate("MID", _TEAM_B, cost=50, horizon_points=500.0)

    scenarios, _ = evaluate_transfer_scenarios(
        squad_candidates=[def_a, def_b, mid_c],
        in_shortlists_by_position={"DEF": [def_med], "MID": [mid_great_1, mid_great_2]},
        selling_prices={def_a.player_id: 50, def_b.player_id: 50, mid_c.player_id: 50},
        team_limit=5,
        bank=1000,  # budget isn't what's under test here
        free_transfers_available=2,
        transfer_hit_cost_magnitude=4,
    )

    scenario2 = scenarios[2]
    assert scenario2.raw_horizon_points_gain == pytest.approx(520.0)
    assert sorted(move.player_out.position_short_name for move in scenario2.moves) == ["DEF", "MID"]
    assert sorted(move.player_in.position_short_name for move in scenario2.moves) == ["DEF", "MID"]


def test_three_transfer_respects_joint_budget_across_all_three():
    """Each swap is individually affordable using the full bank in isolation, but all three
    together exceed combined selling price + bank -- the joint-budget check (already proven for
    2 transfers) must hold for 3 as well.
    """
    out1 = _candidate("DEF", _TEAM_A, cost=50, horizon_points=10.0)
    out2 = _candidate("MID", _TEAM_B, cost=50, horizon_points=10.0)
    out3 = _candidate("FWD", _TEAM_C, cost=50, horizon_points=10.0)
    in1 = _candidate("DEF", _TEAM_D, cost=55, horizon_points=20.0)
    in2 = _candidate("MID", _TEAM_ARS, cost=55, horizon_points=20.0)
    in3 = _candidate("FWD", _TEAM_CHE, cost=55, horizon_points=20.0)
    # Combined: 55*3=165 > 50*3 + 10 = 160 -- jointly infeasible by exactly 5.

    scenarios, _ = evaluate_transfer_scenarios(
        squad_candidates=[out1, out2, out3],
        in_shortlists_by_position={"DEF": [in1], "MID": [in2], "FWD": [in3]},
        selling_prices={out1.player_id: 50, out2.player_id: 50, out3.player_id: 50},
        team_limit=5,
        bank=10,
        free_transfers_available=3,
        transfer_hit_cost_magnitude=4,
    )

    assert scenarios[3].moves == []
    assert scenarios[3].raw_horizon_points_gain == 0.0
