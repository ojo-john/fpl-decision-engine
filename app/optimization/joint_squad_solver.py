"""Joint squad + starting-XI + captain MIP solver: replaces the old two-stage squad_solver.py.

Why the old two-stage approach is wrong: ``squad_solver.py`` maximized total
points across all 15 picks, with zero incentive to keep bench players cheap
— it would happily spend leftover budget on a bench player's marginal point
gain even though that player never starts and their points never count.

A points-only JOINT objective (squad+XI+captain picked together in one MIP)
does NOT fix this either — it is provably indifferent between multiple squads
that tie at the same optimal XI value, since the objective has no term
rewarding cheaper bench players; which tied solution CBC actually returns is
solver-internal, not reproducible.

The fix is a two-phase lexicographic solve:

Phase 1 maximizes (sum of starters' projected points) + (captain_multiplier-1)
* captain's projected points, subject to full squad-legality constraints
(budget/team-limit/per-position squad_select on ``x`` — "in squad") AND full
XI legality constraints (exactly 11 starters, per-position
squad_min_play/squad_max_play on ``y`` — "starting", with ``y_i <= x_i``) AND
captain constraints (``c_i <= y_i``, ``sum(c_i) == 1``). Every objective term
is a known data constant (``projected_points_i``) times a single binary
decision variable — this is standard linear MILP, never bilinear, since no
two decision variables are ever multiplied together.

Phase 2 re-solves the SAME model with the Phase-1-optimal objective value
pinned as a ``>=`` constraint (with a tiny float-safety epsilon, not a real
trade-off), and a new objective: minimize total cost of non-starting squad
members (``sum(now_cost_i * (x_i - y_i))``). This produces genuinely cheap
bench fodder without sacrificing any of the primary points/captain value.

The final captain/vice-captain designation is NOT read off the MIP's ``cap``
variables directly — CBC's choice among objective-value ties is solver-
internal and not reproducible. Instead, exactly like ``lineup_solver.py``,
captain/vice are derived deterministically post-solve via ``rank_key`` over
the actual selected starters (top-ranked = captain, second = vice).
"""

import uuid
from dataclasses import dataclass

import pulp

from app.optimization.bench_order import order_bench
from app.optimization.candidates import OptimizationCandidate
from app.optimization.constraints import OptimizationConstraints
from app.optimization.lineup_solver import STARTING_XI_SIZE, rank_key
from app.optimization.solver_common import map_solver_status
from packages.domain.optimization import SolverStatus

_PHASE1_EPSILON = 1e-6


@dataclass(frozen=True)
class JointSquadSolution:
    solver_status: SolverStatus
    squad_player_ids: list[uuid.UUID]
    starting_player_ids: list[uuid.UUID]
    bench_player_ids_ordered: list[uuid.UUID]  # real FPL bench-slot order [12, 13, 14, 15]
    captain_player_id: uuid.UUID | None
    vice_captain_player_id: uuid.UUID | None
    squad_total_cost: int | None
    squad_projected_points: float | None
    xi_projected_points: float | None


def _empty_solution(status: SolverStatus) -> JointSquadSolution:
    return JointSquadSolution(status, [], [], [], None, None, None, None, None)


def _add_squad_and_xi_constraints(
    prob: pulp.LpProblem,
    x: dict[uuid.UUID, pulp.LpVariable],
    y: dict[uuid.UUID, pulp.LpVariable],
    cap: dict[uuid.UUID, pulp.LpVariable],
    candidates: list[OptimizationCandidate],
    constraints: OptimizationConstraints,
) -> None:
    """Add the full squad-legality + XI-legality + captain constraint set to ``prob``.

    Shared verbatim between Phase 1 and Phase 2 so the two models are
    identical apart from their objective and the Phase-2-only pinned-value
    constraint.
    """
    for c in candidates:
        prob += y[c.player_id] <= x[c.player_id]
        prob += cap[c.player_id] <= y[c.player_id]
    prob += pulp.lpSum(cap.values()) == 1

    # Squad-legality constraints on x (mirrors the old squad_solver.py).
    prob += pulp.lpSum(x.values()) == constraints.squad_size
    for short_name, req in constraints.position_requirements.items():
        prob += (
            pulp.lpSum(x[c.player_id] for c in candidates if c.position_short_name == short_name)
            == req.squad_select
        )
    for team_id in {c.team_id for c in candidates}:
        prob += (
            pulp.lpSum(x[c.player_id] for c in candidates if c.team_id == team_id) <= constraints.team_limit
        )
    prob += pulp.lpSum(c.now_cost * x[c.player_id] for c in candidates) <= constraints.total_spend_limit

    # XI-legality constraints on y (mirrors the old lineup_solver.py).
    prob += pulp.lpSum(y.values()) == STARTING_XI_SIZE
    for short_name, req in constraints.position_requirements.items():
        count = pulp.lpSum(y[c.player_id] for c in candidates if c.position_short_name == short_name)
        prob += count >= req.squad_min_play
        prob += count <= req.squad_max_play


def _points_objective_expr(
    candidates: list[OptimizationCandidate],
    y: dict[uuid.UUID, pulp.LpVariable],
    cap: dict[uuid.UUID, pulp.LpVariable],
    captain_multiplier: int,
) -> pulp.LpAffineExpression:
    return pulp.lpSum(c.projected_points * y[c.player_id] for c in candidates) + (
        captain_multiplier - 1
    ) * pulp.lpSum(c.projected_points * cap[c.player_id] for c in candidates)


def solve_joint_squad_and_xi(
    candidates: list[OptimizationCandidate],
    constraints: OptimizationConstraints,
    captain_multiplier: int,
) -> JointSquadSolution:
    """Two-phase lexicographic MIP solve: max XI+captain points, then min bench cost.

    See module docstring for the full rationale. Returns
    ``SolverStatus.INFEASIBLE``/``ERROR`` with empty fields if either phase
    fails to solve to optimality.
    """
    if not candidates:
        return _empty_solution(SolverStatus.INFEASIBLE)

    # --- Phase 1: maximize XI + captain-bonus projected points ---
    prob1 = pulp.LpProblem("joint_squad_xi_phase1", pulp.LpMaximize)
    x1 = {c.player_id: pulp.LpVariable(f"x_{c.player_id}", cat="Binary") for c in candidates}
    y1 = {c.player_id: pulp.LpVariable(f"y_{c.player_id}", cat="Binary") for c in candidates}
    cap1 = {c.player_id: pulp.LpVariable(f"c_{c.player_id}", cat="Binary") for c in candidates}

    _add_squad_and_xi_constraints(prob1, x1, y1, cap1, candidates, constraints)
    phase1_objective_expr = _points_objective_expr(candidates, y1, cap1, captain_multiplier)
    prob1 += phase1_objective_expr

    prob1.solve(pulp.PULP_CBC_CMD(msg=False))
    status1 = map_solver_status(prob1.status)
    if status1 != SolverStatus.OPTIMAL:
        return _empty_solution(status1)

    phase1_value = pulp.value(phase1_objective_expr)
    assert phase1_value is not None

    # --- Phase 2: re-solve, pin the Phase-1 value, minimize bench cost ---
    prob2 = pulp.LpProblem("joint_squad_xi_phase2", pulp.LpMinimize)
    x2 = {c.player_id: pulp.LpVariable(f"x2_{c.player_id}", cat="Binary") for c in candidates}
    y2 = {c.player_id: pulp.LpVariable(f"y2_{c.player_id}", cat="Binary") for c in candidates}
    cap2 = {c.player_id: pulp.LpVariable(f"c2_{c.player_id}", cat="Binary") for c in candidates}

    _add_squad_and_xi_constraints(prob2, x2, y2, cap2, candidates, constraints)
    phase2_points_expr = _points_objective_expr(candidates, y2, cap2, captain_multiplier)
    prob2 += phase2_points_expr >= phase1_value - _PHASE1_EPSILON

    prob2 += pulp.lpSum(c.now_cost * (x2[c.player_id] - y2[c.player_id]) for c in candidates)

    prob2.solve(pulp.PULP_CBC_CMD(msg=False))
    status2 = map_solver_status(prob2.status)
    if status2 != SolverStatus.OPTIMAL:
        return _empty_solution(status2)

    candidates_by_id = {c.player_id: c for c in candidates}
    squad_ids = {c.player_id for c in candidates if x2[c.player_id].value() == 1}
    starter_ids = {c.player_id for c in candidates if y2[c.player_id].value() == 1}
    bench_ids = squad_ids - starter_ids

    squad_members = [candidates_by_id[pid] for pid in squad_ids]
    starters = sorted((candidates_by_id[pid] for pid in starter_ids), key=rank_key)
    bench_members = [candidates_by_id[pid] for pid in bench_ids]

    starting_position_counts: dict[str, int] = {}
    for c in starters:
        starting_position_counts[c.position_short_name] = (
            starting_position_counts.get(c.position_short_name, 0) + 1
        )
    bench_ordered = order_bench(bench_members, starting_position_counts, constraints.position_requirements)

    captain_id = starters[0].player_id
    vice_captain_id = starters[1].player_id

    non_captain_starters_points = sum(c.projected_points for c in starters if c.player_id != captain_id)
    captain_points = candidates_by_id[captain_id].projected_points
    xi_projected_points = non_captain_starters_points + captain_multiplier * captain_points

    return JointSquadSolution(
        solver_status=SolverStatus.OPTIMAL,
        squad_player_ids=[c.player_id for c in squad_members],
        starting_player_ids=[c.player_id for c in starters],
        bench_player_ids_ordered=[c.player_id for c in bench_ordered],
        captain_player_id=captain_id,
        vice_captain_player_id=vice_captain_id,
        squad_total_cost=sum(c.now_cost for c in squad_members),
        squad_projected_points=sum(c.projected_points for c in squad_members),
        xi_projected_points=xi_projected_points,
    )
