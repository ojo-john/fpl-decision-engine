"""The initial-squad MIP solver: pick 15 players maximizing projected points under FPL's squad rules."""

import uuid
from dataclasses import dataclass

import pulp

from app.optimization.candidates import OptimizationCandidate
from app.optimization.constraints import OptimizationConstraints
from app.optimization.solver_common import map_solver_status
from packages.domain.optimization import SolverStatus


@dataclass(frozen=True)
class SquadSolution:
    solver_status: SolverStatus
    selected_player_ids: list[uuid.UUID]
    total_cost: int | None
    total_projected_points: float | None


def solve_initial_squad(
    candidates: list[OptimizationCandidate], constraints: OptimizationConstraints
) -> SquadSolution:
    """Solve for the 15-player squad maximizing total projected points.

    Budget constraint uses ``now_cost``/``total_spend_limit`` directly (both
    are already in the same integer tenths-of-a-million unit) — no currency
    multiplier arithmetic needed here.
    """
    if not candidates:
        return SquadSolution(SolverStatus.INFEASIBLE, [], None, None)

    prob = pulp.LpProblem("initial_squad", pulp.LpMaximize)
    x = {c.player_id: pulp.LpVariable(f"x_{c.player_id}", cat="Binary") for c in candidates}

    prob += pulp.lpSum(c.projected_points * x[c.player_id] for c in candidates)

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

    prob.solve(pulp.PULP_CBC_CMD(msg=False))

    solver_status = map_solver_status(prob.status)
    if solver_status != SolverStatus.OPTIMAL:
        return SquadSolution(solver_status, [], None, None)

    selected = [c for c in candidates if x[c.player_id].value() == 1]
    return SquadSolution(
        solver_status=SolverStatus.OPTIMAL,
        selected_player_ids=[c.player_id for c in selected],
        total_cost=sum(c.now_cost for c in selected),
        total_projected_points=sum(c.projected_points for c in selected),
    )
