"""Shared ``optimization_run_players`` row-building logic for both job handlers.

Both ``SquadOptimizationJobRunner`` (JOINT_SQUAD_AND_XI) and
``LineupOptimizationJobRunner`` (STARTING_XI) produce the same shape of
recommendation — a full squad with starters/bench/captain/vice-captain — so
this module centralizes building the per-player rows and the
``xi_projected_points`` calculation to avoid two subtly-different copies of
the same logic.
"""

import uuid
from decimal import Decimal

from app.optimization.candidates import OptimizationCandidate
from packages.database.models.optimization_run_player import OptimizationRunPlayer

_BENCH_SLOT_NUMBERS = (12, 13, 14, 15)


def build_run_player_rows_and_xi_points(
    *,
    optimization_run_id: uuid.UUID,
    candidates_by_id: dict[uuid.UUID, OptimizationCandidate],
    starting_player_ids: list[uuid.UUID],
    bench_player_ids_ordered: list[uuid.UUID],
    captain_player_id: uuid.UUID,
    vice_captain_player_id: uuid.UUID,
    captain_multiplier: int,
) -> tuple[list[OptimizationRunPlayer], float]:
    """Build one ``OptimizationRunPlayer`` row per squad member, plus ``xi_projected_points``.

    ``bench_player_ids_ordered`` must already be in real FPL bench-slot
    priority order (see ``app.optimization.bench_order.order_bench``) — this
    function maps that order onto the literal slot numbers 12/13/14/15 stored
    in ``bench_order``.

    ``xi_projected_points`` is the sum of non-captain starters' projected
    points, plus ``captain_multiplier`` times the captain's projected points
    — matching the convention already established by
    ``app/jobs/handlers/lineup_optimization.py``.
    """
    starter_id_set = set(starting_player_ids)

    non_captain_starters_points = sum(
        candidates_by_id[pid].projected_points for pid in starting_player_ids if pid != captain_player_id
    )
    captain_points = candidates_by_id[captain_player_id].projected_points
    xi_projected_points = non_captain_starters_points + captain_multiplier * captain_points

    bench_slot_by_id = dict(zip(bench_player_ids_ordered, _BENCH_SLOT_NUMBERS, strict=True))

    run_player_rows = []
    for pid in [*starting_player_ids, *bench_player_ids_ordered]:
        candidate = candidates_by_id[pid]
        run_player_rows.append(
            OptimizationRunPlayer(
                optimization_run_id=optimization_run_id,
                player_id=pid,
                is_starting=pid in starter_id_set,
                bench_order=bench_slot_by_id.get(pid),
                is_captain=(pid == captain_player_id),
                is_vice_captain=(pid == vice_captain_player_id),
                projected_points=Decimal(str(candidate.projected_points)),
                projection_source=candidate.projection_source,
                now_cost_snapshot=candidate.now_cost,
            )
        )
    return run_player_rows, xi_projected_points
