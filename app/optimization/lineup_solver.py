"""The starting-XI MIP solver: pick 11 starters (+ordered bench, captain, vice) from a fixed 15-player squad.

A second, small MIP over the fixed squad — objective is plain summed
projected points (not captain-doubled); captain/vice selection is a
deterministic post-solve ranking, not part of the MIP itself. Bench ordering
is delegated to ``app.optimization.bench_order.order_bench`` — real FPL
bench-slot order (12=reserve GK, 13-15=breadth-ascending outfield priority),
not plain descending points (see that module's docstring for why).
"""

import uuid
from dataclasses import dataclass

import pulp

from app.optimization.bench_order import order_bench
from app.optimization.candidates import OptimizationCandidate
from app.optimization.constraints import PositionRequirement
from app.optimization.solver_common import map_solver_status
from packages.domain.optimization import SolverStatus

STARTING_XI_SIZE = 11


@dataclass(frozen=True)
class LineupSolution:
    solver_status: SolverStatus
    starting_player_ids: list[uuid.UUID]  # ranked descending by projected_points (captain first)
    bench_player_ids_ordered: list[uuid.UUID]  # real FPL bench-slot order [12, 13, 14, 15]
    captain_player_id: uuid.UUID | None
    vice_captain_player_id: uuid.UUID | None


def rank_key(candidate: OptimizationCandidate) -> tuple[float, int, str]:
    """Descending by projected_points, tie-broken by higher now_cost, then player_id string.

    Every value is negated/stringified so a plain ascending ``sorted()`` call
    produces the desired descending, fully reproducible order. Public (not
    underscore-prefixed) so ``app.optimization.joint_squad_solver`` can reuse
    the exact same captain/vice-captain tie-break convention.
    """
    return (-candidate.projected_points, -candidate.now_cost, str(candidate.player_id))


def solve_lineup(
    squad_candidates: list[OptimizationCandidate],
    position_requirements: dict[str, PositionRequirement],
) -> LineupSolution:
    """Solve for the best starting XI (by plain projected points) from a fixed 15-player squad."""
    if not squad_candidates:
        return LineupSolution(SolverStatus.INFEASIBLE, [], [], None, None)

    prob = pulp.LpProblem("lineup", pulp.LpMaximize)
    y = {c.player_id: pulp.LpVariable(f"y_{c.player_id}", cat="Binary") for c in squad_candidates}

    prob += pulp.lpSum(c.projected_points * y[c.player_id] for c in squad_candidates)
    prob += pulp.lpSum(y.values()) == STARTING_XI_SIZE

    for short_name, req in position_requirements.items():
        count = pulp.lpSum(y[c.player_id] for c in squad_candidates if c.position_short_name == short_name)
        prob += count >= req.squad_min_play
        prob += count <= req.squad_max_play

    prob.solve(pulp.PULP_CBC_CMD(msg=False))

    solver_status = map_solver_status(prob.status)
    if solver_status != SolverStatus.OPTIMAL:
        return LineupSolution(solver_status, [], [], None, None)

    starter_ids = {c.player_id for c in squad_candidates if y[c.player_id].value() == 1}
    starters = sorted((c for c in squad_candidates if c.player_id in starter_ids), key=rank_key)
    bench = [c for c in squad_candidates if c.player_id not in starter_ids]

    starting_position_counts: dict[str, int] = {}
    for c in starters:
        starting_position_counts[c.position_short_name] = (
            starting_position_counts.get(c.position_short_name, 0) + 1
        )
    bench_ordered = order_bench(bench, starting_position_counts, position_requirements)

    return LineupSolution(
        solver_status=SolverStatus.OPTIMAL,
        starting_player_ids=[c.player_id for c in starters],
        bench_player_ids_ordered=[c.player_id for c in bench_ordered],
        captain_player_id=starters[0].player_id,
        vice_captain_player_id=starters[1].player_id,
    )
