"""Unit tests for app.chips.schedule_solver.solve_chip_schedule.

Pure function, no DB -- covers this repo's own standing priority order (failure paths/boundary
conditions before happy paths): no candidates at all, every candidate a losing (non-positive) gain,
and the actual real-world bug this module exists to fix -- a forced collision where two chips'
candidate lists fully overlap and the solver must pick the higher-value one, never both.
"""

import uuid

from app.chips.schedule_solver import ChipScheduleCandidateInput, solve_chip_schedule
from packages.domain.chips import ChipType
from packages.domain.optimization import SolverStatus


def _candidate(chip_type: ChipType, window_number: int, fpl_event_id: int, gain_mean: float):
    return ChipScheduleCandidateInput(
        candidate_id=uuid.uuid4(),
        chip_type=chip_type,
        window_number=window_number,
        fpl_event_id=fpl_event_id,
        gain_mean=gain_mean,
    )


def test_solve_chip_schedule_no_candidates_returns_optimal_empty_solution():
    solution = solve_chip_schedule([])

    assert solution.solver_status == SolverStatus.OPTIMAL
    assert solution.assignments == []
    assert solution.total_expected_gain == 0.0


def test_solve_chip_schedule_all_negative_gains_leaves_every_slot_unassigned():
    candidates = [
        _candidate(ChipType.BBOOST, 1, 10, -3.0),
        _candidate(ChipType.BBOOST, 1, 11, -1.0),
        _candidate(ChipType.TRIPLE_CAPTAIN, 1, 10, -2.0),
    ]

    solution = solve_chip_schedule(candidates)

    assert solution.solver_status == SolverStatus.OPTIMAL
    assert len(solution.assignments) == 2
    for assignment in solution.assignments:
        assert assignment.assigned_fpl_event_id is None
        assert assignment.gain_mean is None
        assert assignment.source_candidate_id is None
    assert solution.total_expected_gain == 0.0


def test_solve_chip_schedule_never_assigns_two_chips_to_the_same_gameweek():
    """The real, live bug this module fixes: freehit/bboost/3xc all independently recommending
    GW2. Every candidate here only exists at GW2 -- the solver must pick exactly the single
    highest-gain chip for GW2 and leave the other two genuinely unassigned, never all three."""
    candidates = [
        _candidate(ChipType.FREEHIT, 1, 2, 3.3),
        _candidate(ChipType.BBOOST, 1, 2, 5.1),
        _candidate(ChipType.TRIPLE_CAPTAIN, 1, 2, 7.3),
    ]

    solution = solve_chip_schedule(candidates)

    assert solution.solver_status == SolverStatus.OPTIMAL
    assigned = [a for a in solution.assignments if a.assigned_fpl_event_id is not None]
    assert len(assigned) == 1
    assert assigned[0].chip_type == ChipType.TRIPLE_CAPTAIN
    assert assigned[0].assigned_fpl_event_id == 2
    assert assigned[0].gain_mean == 7.3
    assert solution.total_expected_gain == 7.3


def test_solve_chip_schedule_picks_the_combination_maximizing_total_gain_not_greedy_per_chip():
    """Wildcard's own best candidate (GW4, gain 20) collides with Bench Boost's own best (GW4,
    gain 3) -- picking each chip's individually-best candidate would be infeasible (same
    gameweek). The optimal total-value combination is Wildcard@GW4 (20) + Bench Boost@GW6 (2.5) =
    22.5, strictly better than Wildcard@GW6 (18) + Bench Boost@GW4 (3) = 21."""
    candidates = [
        _candidate(ChipType.WILDCARD, 1, 4, 20.0),
        _candidate(ChipType.WILDCARD, 1, 6, 18.0),
        _candidate(ChipType.BBOOST, 1, 4, 3.0),
        _candidate(ChipType.BBOOST, 1, 6, 2.5),
    ]

    solution = solve_chip_schedule(candidates)

    assert solution.solver_status == SolverStatus.OPTIMAL
    by_chip = {a.chip_type: a for a in solution.assignments}
    assert by_chip[ChipType.WILDCARD].assigned_fpl_event_id == 4
    assert by_chip[ChipType.BBOOST].assigned_fpl_event_id == 6
    assert solution.total_expected_gain == 22.5


def test_solve_chip_schedule_leaves_unrelated_gameweeks_and_slots_independent():
    candidates = [
        _candidate(ChipType.WILDCARD, 1, 4, 20.0),
        _candidate(ChipType.FREEHIT, 1, 10, 5.0),
    ]

    solution = solve_chip_schedule(candidates)

    assert solution.solver_status == SolverStatus.OPTIMAL
    by_chip = {a.chip_type: a for a in solution.assignments}
    assert by_chip[ChipType.WILDCARD].assigned_fpl_event_id == 4
    assert by_chip[ChipType.FREEHIT].assigned_fpl_event_id == 10
    assert solution.total_expected_gain == 25.0


def test_solve_chip_schedule_traces_the_winning_candidate_id_for_persistence():
    winning_id = uuid.uuid4()
    candidates = [
        ChipScheduleCandidateInput(winning_id, ChipType.TRIPLE_CAPTAIN, 1, 2, 7.3),
    ]

    solution = solve_chip_schedule(candidates)

    assert solution.assignments[0].source_candidate_id == winning_id
