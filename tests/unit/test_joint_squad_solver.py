"""Unit tests for app.optimization.joint_squad_solver.solve_joint_squad_and_xi.

Reproduces the toy pool from the design: 6 candidates padded into a single
trivial "position" (since OptimizationConstraints/PositionRequirement require
per-position structure, but the mechanics of the worked example don't need
real position distinctions) — squad_size=4, starting=2 (via
squad_min_play=squad_max_play=2 for the sole position), budget=25,
captain_multiplier=2.

S1(cost10,pts10) / S2(cost9,pts8.5) / B1(cost5,pts5) / B2(cost3,pts3) /
F1(cost2,pts2) / F2(cost1,pts0.5).

The two-stage approach (maximize points across all 4 squad picks with no
bench-cost incentive) would pick {S1,S2,B1,F2} at cost 25 (spending 3 units
upgrading a bench player, B1, that never starts). The joint two-phase solver
must instead pick {S1,S2,F1,F2} at cost 22 — same optimal XI value (S1+S2
starting, captain=S1) but genuinely cheap bench.
"""

import uuid

import pytest

import app.optimization.joint_squad_solver as joint_squad_solver_module
from app.optimization.candidates import OptimizationCandidate
from app.optimization.constraints import OptimizationConstraints, PositionRequirement
from app.optimization.joint_squad_solver import solve_joint_squad_and_xi
from packages.domain.optimization import SolverStatus


@pytest.fixture(autouse=True)
def _toy_starting_xi_size(monkeypatch):
    """The real ``STARTING_XI_SIZE`` (11) is a genuine FPL constant, not something
    ``OptimizationConstraints`` carries — so to exercise the joint solver's
    phase-1/phase-2 mechanics against the design's minimal toy pool (squad_size=4,
    starting=2), patch the module's imported constant down to 2 for this test
    module only. Production code is unaffected; this only changes what this
    test file's calls to ``solve_joint_squad_and_xi`` see.
    """
    monkeypatch.setattr(joint_squad_solver_module, "STARTING_XI_SIZE", 2)


_TEAM_A = uuid.uuid4()
_TEAM_B = uuid.uuid4()
_RULES_CONFIG_ID = uuid.uuid4()
_POSITION_ID = uuid.uuid4()


def _candidate(player_id: uuid.UUID, projected_points: float, now_cost: int) -> OptimizationCandidate:
    return OptimizationCandidate(
        player_id=player_id,
        projected_points=projected_points,
        projection_source="test",
        position_short_name="ALL",
        team_id=_TEAM_A,
        now_cost=now_cost,
    )


def _toy_pool() -> dict[str, OptimizationCandidate]:
    return {
        "S1": _candidate(uuid.uuid4(), 10.0, 10),
        "S2": _candidate(uuid.uuid4(), 8.5, 9),
        "B1": _candidate(uuid.uuid4(), 5.0, 5),
        "B2": _candidate(uuid.uuid4(), 3.0, 3),
        "F1": _candidate(uuid.uuid4(), 2.0, 2),
        "F2": _candidate(uuid.uuid4(), 0.5, 1),
    }


def _constraints(total_spend_limit: int, team_limit: int = 10) -> OptimizationConstraints:
    return OptimizationConstraints(
        rules_config_id=_RULES_CONFIG_ID,
        squad_size=4,
        team_limit=team_limit,
        total_spend_limit=total_spend_limit,
        currency_multiplier=10,
        position_requirements={
            "ALL": PositionRequirement(
                position_id=_POSITION_ID,
                short_name="ALL",
                squad_select=4,
                squad_min_play=2,
                squad_max_play=2,
            )
        },
    )


def test_joint_solver_prefers_cheap_bench_over_two_stage_result():
    pool = _toy_pool()
    candidates = list(pool.values())
    constraints = _constraints(total_spend_limit=25)

    solution = solve_joint_squad_and_xi(candidates, constraints, captain_multiplier=2)

    assert solution.solver_status == SolverStatus.OPTIMAL
    assert set(solution.squad_player_ids) == {
        pool["S1"].player_id,
        pool["S2"].player_id,
        pool["F1"].player_id,
        pool["F2"].player_id,
    }
    assert solution.squad_total_cost == 22
    assert set(solution.starting_player_ids) == {pool["S1"].player_id, pool["S2"].player_id}
    assert solution.captain_player_id == pool["S1"].player_id
    assert solution.vice_captain_player_id == pool["S2"].player_id
    # XI value: 8.5 (S2, non-captain) + 2 * 10 (S1, captain) = 28.5
    assert solution.xi_projected_points == pytest.approx(28.5)
    # Bench players never start — confirms bench_player_ids_ordered covers F1/F2.
    assert set(solution.bench_player_ids_ordered) == {pool["F1"].player_id, pool["F2"].player_id}


def test_joint_solver_returns_infeasible_when_budget_too_small():
    pool = _toy_pool()
    candidates = list(pool.values())
    # Even the 4 cheapest candidates (F2=1, F1=2, B2=3, B1=5 => 11) exceed a
    # budget of 5, so no legal 4-player squad can be formed at all.
    constraints = _constraints(total_spend_limit=5)

    solution = solve_joint_squad_and_xi(candidates, constraints, captain_multiplier=2)

    assert solution.solver_status == SolverStatus.INFEASIBLE
    assert solution.squad_player_ids == []
    assert solution.starting_player_ids == []
    assert solution.bench_player_ids_ordered == []
    assert solution.captain_player_id is None
    assert solution.vice_captain_player_id is None


def test_joint_solver_handles_empty_candidate_pool():
    constraints = _constraints(total_spend_limit=25)
    solution = solve_joint_squad_and_xi([], constraints, captain_multiplier=2)
    assert solution.solver_status == SolverStatus.INFEASIBLE
