"""Phase 1 of the joint chip scheduler: a pure PuLP assignment MIP that picks the best
non-colliding gameweek-to-chip assignment across all 4 chips, given each chip's own
ALREADY-COMPUTED, independently evaluated candidate gains.

Real bug this exists to fix: ``app.chips.scenario``/``app.jobs.handlers.chip_simulation``
evaluate each of the 4 FPL chips (Wildcard, Free Hit, Bench Boost, Triple Captain) completely
independently -- confirmed live: the tool recommended "use now" for 3 different chips in the SAME
gameweek. A real FPL manager can only play ONE chip per gameweek. This module adds a second layer
on top, NOT a replacement: it consumes the 4 chips' own already-persisted
``chip_window_candidates`` rows (one ``ChipScheduleCandidateInput`` per row) as FIXED inputs and
solves a small assignment problem -- "which single candidate gameweek, if any, should each chip
use, such that no two chips share a gameweek, maximizing total expected gain."

Mirrors ``app.optimization.joint_squad_solver``'s house style: binary ``LpVariable`` per decision,
``lpSum(...) <= N`` constraints, ``pulp.PULP_CBC_CMD(msg=False)``, status mapped via
``app.optimization.solver_common.map_solver_status`` onto the shared ``SolverStatus`` enum (never
a bespoke status type for this one solver).

**Deliberately NOT modeled here** (Phase 2's job, not this module's): compounding effects between
chips -- e.g. a Wildcard rebuild permanently changes the squad, which changes what Bench
Boost/Triple Captain/a later Free Hit are actually worth in gameweeks after it. Every input gain
here is treated as a fixed, independent point estimate, exactly as ``CHIP_SIMULATION`` already
computed it, with no re-evaluation. This is a real, deliberate scope boundary, not an oversight --
see ``app/jobs/handlers/chip_schedule_optimization.py``'s own module docstring for the full
Phase 1 vs. Phase 2 split.
"""

import uuid
from dataclasses import dataclass

import pulp

from app.optimization.solver_common import map_solver_status
from packages.domain.chips import ChipType
from packages.domain.optimization import SolverStatus


@dataclass(frozen=True)
class ChipScheduleCandidateInput:
    """One (chip_type, window_number, candidate gameweek) input to the assignment solve -- mirrors
    one ``chip_window_candidates`` row exactly. ``candidate_id`` is that row's own id, carried
    through only for traceability in the persisted result (``ChipScheduleAssignmentResult
    .source_candidate_id``) -- the solver itself never reads it."""

    candidate_id: uuid.UUID
    chip_type: ChipType
    window_number: int
    fpl_event_id: int
    gain_mean: float


@dataclass(frozen=True)
class ChipScheduleAssignmentResult:
    """The solver's final decision for one (chip_type, window_number) slot. ``assigned_fpl_event_id``
    /``gain_mean``/``source_candidate_id`` are all ``None`` together when the solver determined this
    chip is not worth playing within the given candidates -- a real, valid outcome (every one of its
    candidates had non-positive gain, or a higher-total-value combination leaves it unused), never
    a partial/error state."""

    chip_type: ChipType
    window_number: int
    assigned_fpl_event_id: int | None
    gain_mean: float | None
    source_candidate_id: uuid.UUID | None


@dataclass(frozen=True)
class ChipScheduleSolution:
    solver_status: SolverStatus
    assignments: list[ChipScheduleAssignmentResult]
    total_expected_gain: float | None


def _empty_slot_assignments(
    slots: list[tuple[ChipType, int]],
) -> list[ChipScheduleAssignmentResult]:
    return [
        ChipScheduleAssignmentResult(chip_type, window_number, None, None, None)
        for chip_type, window_number in slots
    ]


def solve_chip_schedule(candidates: list[ChipScheduleCandidateInput]) -> ChipScheduleSolution:
    """Solve the assignment MIP. Returns one ``ChipScheduleAssignmentResult`` per distinct
    (chip_type, window_number) slot present in ``candidates`` -- including slots that end up
    unassigned. Never raises: a genuinely infeasible/error solve (should not occur for this
    problem shape -- every constraint here is a simple ``<= 1`` count, never conflicting by
    construction) is reported via ``solver_status``, not an exception, mirroring every other
    solver in this codebase.
    """
    slots = sorted({(c.chip_type, c.window_number) for c in candidates}, key=lambda s: (s[0].value, s[1]))

    if not candidates:
        return ChipScheduleSolution(SolverStatus.OPTIMAL, [], 0.0)

    prob = pulp.LpProblem("chip_schedule", pulp.LpMaximize)

    x: dict[uuid.UUID, pulp.LpVariable] = {
        c.candidate_id: pulp.LpVariable(f"x_{c.candidate_id.hex}", cat="Binary") for c in candidates
    }

    # At most one candidate gameweek chosen per (chip_type, window_number) slot -- a chip is only
    # left unassigned if the solver determines that's genuinely optimal (every one of its own
    # candidates has non-positive gain, or using one would collide with a more valuable use of
    # that gameweek by a different chip). Never forced to pick a losing candidate.
    for chip_type, window_number in slots:
        prob += (
            pulp.lpSum(
                x[c.candidate_id]
                for c in candidates
                if c.chip_type == chip_type and c.window_number == window_number
            )
            <= 1
        )

    # At most one chip played per real gameweek, across every chip/window -- the actual
    # mutual-exclusivity constraint this whole module exists to enforce.
    for fpl_event_id in {c.fpl_event_id for c in candidates}:
        prob += pulp.lpSum(x[c.candidate_id] for c in candidates if c.fpl_event_id == fpl_event_id) <= 1

    prob += pulp.lpSum(x[c.candidate_id] * c.gain_mean for c in candidates)

    prob.solve(pulp.PULP_CBC_CMD(msg=False))
    status = map_solver_status(prob.status)

    if status != SolverStatus.OPTIMAL:
        return ChipScheduleSolution(status, _empty_slot_assignments(slots), None)

    chosen_by_slot: dict[tuple[ChipType, int], ChipScheduleCandidateInput] = {}
    for c in candidates:
        if x[c.candidate_id].value() == 1:
            chosen_by_slot[(c.chip_type, c.window_number)] = c

    assignments: list[ChipScheduleAssignmentResult] = []
    total_expected_gain = 0.0
    for chip_type, window_number in slots:
        chosen = chosen_by_slot.get((chip_type, window_number))
        if chosen is None:
            assignments.append(ChipScheduleAssignmentResult(chip_type, window_number, None, None, None))
            continue
        assignments.append(
            ChipScheduleAssignmentResult(
                chip_type, window_number, chosen.fpl_event_id, chosen.gain_mean, chosen.candidate_id
            )
        )
        total_expected_gain += chosen.gain_mean

    return ChipScheduleSolution(status, assignments, total_expected_gain)
