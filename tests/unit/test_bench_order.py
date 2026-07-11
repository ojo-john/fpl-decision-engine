"""Unit tests for app.optimization.bench_order.order_bench / breadth.

Reproduces the MID_Y/FWD_Z1/FWD_Z2 worked example from the design: a 5-4-1
formation (DEF=5 at max, MID=4, FWD=1 at min; rules DEF 3-5/MID 2-5/FWD 1-3)
where plain points-descending bench ordering is provably suboptimal versus
breadth-ascending (most-constrained-eligibility-first) ordering.
"""

import uuid

import pytest

from app.optimization.bench_order import order_bench
from app.optimization.candidates import OptimizationCandidate
from app.optimization.constraints import PositionRequirement

_TEAM_ID = uuid.uuid4()


def _candidate(
    position_short_name: str, projected_points: float, now_cost: int = 45
) -> OptimizationCandidate:
    return OptimizationCandidate(
        player_id=uuid.uuid4(),
        projected_points=projected_points,
        projection_source="test",
        position_short_name=position_short_name,
        team_id=_TEAM_ID,
        now_cost=now_cost,
    )


def _position_requirements(
    *,
    gkp: tuple[int, int, int] = (2, 1, 1),
    def_: tuple[int, int, int] = (5, 3, 5),
    mid: tuple[int, int, int] = (5, 2, 5),
    fwd: tuple[int, int, int] = (3, 1, 3),
) -> dict[str, PositionRequirement]:
    specs = {"GKP": gkp, "DEF": def_, "MID": mid, "FWD": fwd}
    return {
        short_name: PositionRequirement(
            position_id=uuid.uuid4(),
            short_name=short_name,
            squad_select=select,
            squad_min_play=min_play,
            squad_max_play=max_play,
        )
        for short_name, (select, min_play, max_play) in specs.items()
    }


def test_worked_example_breadth_ascending_beats_points_descending():
    """5-4-1 formation, DEF at max (5) and FWD at min (1): the narrowest-eligibility
    bench player (MID_Y) must be placed ahead of the higher-projected but
    broader-eligibility FWD_Z1, contradicting plain points-descending order.
    """
    position_requirements = _position_requirements()
    starting_position_counts = {"GKP": 1, "DEF": 5, "MID": 4, "FWD": 1}

    gk_bench = _candidate("GKP", 2.0)
    mid_y = _candidate("MID", 8.0)
    fwd_z1 = _candidate("FWD", 10.0)
    fwd_z2 = _candidate("FWD", 1.0)

    bench_candidates = [gk_bench, mid_y, fwd_z1, fwd_z2]
    ordered = order_bench(bench_candidates, starting_position_counts, position_requirements)

    assert [c.player_id for c in ordered] == [
        gk_bench.player_id,
        mid_y.player_id,
        fwd_z1.player_id,
        fwd_z2.player_id,
    ]

    # Plain points-descending would have put FWD_Z1 (10pts) at slot 13 ahead of
    # MID_Y (8pts) — that is the provably wrong ordering this fix replaces.
    points_descending_order = sorted([mid_y, fwd_z1, fwd_z2], key=lambda c: -c.projected_points)
    assert points_descending_order[0].player_id == fwd_z1.player_id
    assert ordered[1].player_id != points_descending_order[0].player_id


def test_gk_always_at_slot_12_regardless_of_projected_points():
    position_requirements = _position_requirements()
    starting_position_counts = {"GKP": 1, "DEF": 5, "MID": 4, "FWD": 1}

    gk_bench = _candidate("GKP", 100.0)  # absurdly high points, must still be slot 12
    mid_y = _candidate("MID", 8.0)
    fwd_z1 = _candidate("FWD", 10.0)
    fwd_z2 = _candidate("FWD", 1.0)

    ordered = order_bench([gk_bench, mid_y, fwd_z1, fwd_z2], starting_position_counts, position_requirements)
    assert ordered[0].player_id == gk_bench.player_id


def test_degenerate_fixed_formation_collapses_to_points_descending():
    """When every position has squad_min_play == squad_max_play (a rigid formation),
    breadth is uniformly equal across all outfield candidates, so the ordering
    collapses to plain descending projected_points.
    """
    position_requirements = _position_requirements(
        gkp=(2, 1, 1), def_=(5, 5, 5), mid=(5, 5, 5), fwd=(3, 1, 1)
    )
    starting_position_counts = {"GKP": 1, "DEF": 5, "MID": 5, "FWD": 1}

    gk_bench = _candidate("GKP", 2.0)
    a = _candidate("DEF", 9.0)
    b = _candidate("MID", 6.0)
    c = _candidate("FWD", 3.0)

    ordered = order_bench([gk_bench, a, b, c], starting_position_counts, position_requirements)
    assert [x.player_id for x in ordered] == [gk_bench.player_id, a.player_id, b.player_id, c.player_id]


def test_deterministic_on_ties():
    position_requirements = _position_requirements(
        gkp=(2, 1, 1), def_=(5, 5, 5), mid=(5, 5, 5), fwd=(3, 1, 1)
    )
    starting_position_counts = {"GKP": 1, "DEF": 5, "MID": 5, "FWD": 1}

    gk_bench = _candidate("GKP", 2.0)
    a = _candidate("DEF", 5.0, now_cost=40)
    b = _candidate("MID", 5.0, now_cost=40)
    c = _candidate("FWD", 5.0, now_cost=40)

    bench_candidates = [gk_bench, a, b, c]
    first = order_bench(bench_candidates, starting_position_counts, position_requirements)
    second = order_bench(bench_candidates, starting_position_counts, position_requirements)
    assert [x.player_id for x in first] == [x.player_id for x in second]


def test_asserts_on_malformed_bench_composition():
    position_requirements = _position_requirements()
    starting_position_counts = {"GKP": 1, "DEF": 5, "MID": 4, "FWD": 1}

    # Two GKPs, no third outfield player — not a legal bench composition.
    gk1 = _candidate("GKP", 2.0)
    gk2 = _candidate("GKP", 3.0)
    mid_y = _candidate("MID", 8.0)
    fwd_z1 = _candidate("FWD", 10.0)

    with pytest.raises(AssertionError):
        order_bench([gk1, gk2, mid_y, fwd_z1], starting_position_counts, position_requirements)
