"""The joint chip scheduler's assignment MIP: picks the best non-colliding gameweek-to-chip
assignment across all 4 chips, given each chip's own candidate gains.

Real bug this exists to fix: ``app.chips.scenario``/``app.jobs.handlers.chip_simulation``
evaluate each of the 4 FPL chips (Wildcard, Free Hit, Bench Boost, Triple Captain) completely
independently -- confirmed live: the tool recommended "use now" for 3 different chips in the SAME
gameweek. A real FPL manager can only play ONE chip per gameweek. This module solves the actual
assignment problem on top of those per-chip evaluations: "which single candidate gameweek, if
any, should each chip use, such that no two chips share a gameweek, maximizing total expected
gain."

Mirrors ``app.optimization.joint_squad_solver``'s house style: binary ``LpVariable`` per decision,
``lpSum(...) <= N`` constraints, ``pulp.PULP_CBC_CMD(msg=False)``, status mapped via
``app.optimization.solver_common.map_solver_status`` onto the shared ``SolverStatus`` enum (never
a bespoke status type for this one solver).

**Phase 2 -- path dependence via candidate "variants", not a separate dynamic program.** Real
finding while designing this: a naive per-chip-independent gain estimate double-counts a Wildcard's
own effect -- Wildcard permanently rebuilds the squad, which changes what Bench Boost/Triple
Captain/a later Free Hit/a later Wildcard window are actually worth in gameweeks AFTER it (Free
Hit's own squad change, by contrast, reverts the following gameweek -- never a path-dependence
trigger). Rather than a bespoke chronological DP, this generalizes the SAME assignment MIP: every
OTHER chip's candidate that falls after a given Wildcard candidate gets an optional
``ChipSchedulePostWildcardVariant`` -- an alternate gain, computed against the hypothetical
post-Wildcard-rebuild squad (see ``app.jobs.handlers.chip_simulation._post_wildcard_evaluation
_context``), whose own binary variable is linked to that Wildcard candidate's own via a standard
implication constraint (``variant_var <= wildcard_var`` -- the exact same idiom
``joint_squad_solver.py`` already uses for captain/starting-XI dependencies, e.g. ``y[player] <=
x[player]``). A variant can only be "active" in the solution if its own required Wildcard candidate
is ALSO chosen -- the solver can never claim a post-Wildcard bonus without actually playing
Wildcard there.

**Stated scope boundary, not silently overclaimed**: only a Wildcard candidate from the season's
FIRST occurring window ever triggers variants for later candidates (see
``app/jobs/handlers/chip_schedule_optimization.py``'s own module docstring for exactly why and
the real compute-cost reasoning) -- a second Wildcard's own value is never itself re-evaluated
against a hypothetical first-Wildcard rebuild. Modeling that deeper, transitively-chained
compounding is real, separate, larger future work, not built here.
"""

import uuid
from dataclasses import dataclass

import pulp

from app.optimization.solver_common import map_solver_status
from packages.domain.chips import ChipType
from packages.domain.optimization import SolverStatus


@dataclass(frozen=True)
class ChipScheduleCandidateInput:
    """One (chip_type, window_number, candidate gameweek) BASELINE input to the assignment solve --
    the gain assuming no Wildcard was played before it (i.e. exactly what ``app.chips.scenario``
    computes against the manager's real current squad). ``candidate_id`` is a unique key for this
    candidate -- for Phase 1 (consuming a persisted ``CHIP_SIMULATION`` run) this was a real
    ``chip_window_candidates`` row id; for Phase 2's own fresh, in-memory evaluation these
    candidates are never separately persisted, so any unique id serves the same purpose (the
    solver itself never distinguishes the two)."""

    candidate_id: uuid.UUID
    chip_type: ChipType
    window_number: int
    fpl_event_id: int
    gain_mean: float


@dataclass(frozen=True)
class ChipSchedulePostWildcardVariant:
    """A path-dependent ALTERNATE gain for one (chip_type, window_number, gameweek) candidate --
    valid ONLY if ``requires_wildcard_candidate_id`` (some OTHER, chronologically EARLIER
    ``ChipScheduleCandidateInput`` whose own ``chip_type == ChipType.WILDCARD``) is ALSO chosen in
    the final assignment. Building a variant for a gameweek that is not genuinely later than its
    own required Wildcard candidate's gameweek is a caller contract this module does not itself
    verify (the solver's own gameweek mutual-exclusivity constraint would simply prevent both from
    ever being chosen together in that degenerate case, but constructing one is never meaningful)."""

    variant_id: uuid.UUID
    chip_type: ChipType
    window_number: int
    fpl_event_id: int
    gain_mean: float
    requires_wildcard_candidate_id: uuid.UUID


@dataclass(frozen=True)
class ChipScheduleAssignmentResult:
    """The solver's final decision for one (chip_type, window_number) slot. ``assigned_fpl_event_id``
    /``gain_mean``/``source_candidate_id`` are all ``None`` together when the solver determined this
    chip is not worth playing within the given candidates -- a real, valid outcome (every one of its
    candidates had non-positive gain, or a higher-total-value combination leaves it unused), never
    a partial/error state. ``source_candidate_id`` is always ``None`` when a WINNING VARIANT (not a
    baseline candidate) was chosen for this slot -- a variant has no persisted
    ``chip_window_candidates`` row of its own to trace back to (see ``ChipScheduleCandidateInput``'s
    own docstring)."""

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


def solve_chip_schedule(
    candidates: list[ChipScheduleCandidateInput],
    variants: list[ChipSchedulePostWildcardVariant] | None = None,
) -> ChipScheduleSolution:
    """Solve the assignment MIP. Returns one ``ChipScheduleAssignmentResult`` per distinct
    (chip_type, window_number) slot present across ``candidates``/``variants`` -- including slots
    that end up unassigned. ``variants`` defaults to none (Phase 1's exact original behavior,
    unchanged for any caller that doesn't pass any). Never raises: a genuinely infeasible/error
    solve (should not occur for this problem shape -- every constraint here is a simple ``<= 1``
    count or a standard implication, never conflicting by construction) is reported via
    ``solver_status``, not an exception, mirroring every other solver in this codebase.
    """
    variants = variants or []
    slots = sorted(
        {(c.chip_type, c.window_number) for c in candidates}
        | {(variant.chip_type, variant.window_number) for variant in variants},
        key=lambda s: (s[0].value, s[1]),
    )

    if not candidates and not variants:
        return ChipScheduleSolution(SolverStatus.OPTIMAL, [], 0.0)

    prob = pulp.LpProblem("chip_schedule", pulp.LpMaximize)

    x: dict[uuid.UUID, pulp.LpVariable] = {
        c.candidate_id: pulp.LpVariable(f"x_{c.candidate_id.hex}", cat="Binary") for c in candidates
    }
    v: dict[uuid.UUID, pulp.LpVariable] = {
        variant.variant_id: pulp.LpVariable(f"v_{variant.variant_id.hex}", cat="Binary")
        for variant in variants
    }

    def _decision_vars_for_slot(chip_type: ChipType, window_number: int) -> list[pulp.LpVariable]:
        return [
            x[c.candidate_id]
            for c in candidates
            if c.chip_type == chip_type and c.window_number == window_number
        ] + [
            v[variant.variant_id]
            for variant in variants
            if variant.chip_type == chip_type and variant.window_number == window_number
        ]

    # At most one candidate (baseline OR variant) chosen per (chip_type, window_number) slot -- a
    # chip is only left unassigned if the solver determines that's genuinely optimal. Never forced
    # to pick a losing candidate.
    for chip_type, window_number in slots:
        prob += pulp.lpSum(_decision_vars_for_slot(chip_type, window_number)) <= 1

    # At most one chip played per real gameweek, across every chip/window AND every variant -- the
    # actual mutual-exclusivity constraint this whole module exists to enforce.
    all_fpl_event_ids = {c.fpl_event_id for c in candidates} | {variant.fpl_event_id for variant in variants}
    for fpl_event_id in all_fpl_event_ids:
        vars_for_gameweek = [x[c.candidate_id] for c in candidates if c.fpl_event_id == fpl_event_id] + [
            v[variant.variant_id] for variant in variants if variant.fpl_event_id == fpl_event_id
        ]
        prob += pulp.lpSum(vars_for_gameweek) <= 1

    # Implication: a variant's post-Wildcard bonus can only be claimed if its own required
    # Wildcard candidate is ALSO chosen -- mirrors joint_squad_solver.py's own `y[p] <= x[p]`
    # dependency idiom exactly.
    for variant in variants:
        required_var = x.get(variant.requires_wildcard_candidate_id)
        if required_var is None:
            # Defensive: a variant referencing a Wildcard candidate that wasn't actually provided
            # in `candidates` -- force it permanently off rather than silently allowing an
            # impossible claim (should never happen given a correct caller, per this dataclass's
            # own docstring contract, but never trust that silently).
            prob += v[variant.variant_id] == 0
            continue
        prob += v[variant.variant_id] <= required_var

    prob += pulp.lpSum([x[c.candidate_id] * c.gain_mean for c in candidates]) + pulp.lpSum(
        [v[variant.variant_id] * variant.gain_mean for variant in variants]
    )

    prob.solve(pulp.PULP_CBC_CMD(msg=False))
    status = map_solver_status(prob.status)

    if status != SolverStatus.OPTIMAL:
        return ChipScheduleSolution(status, _empty_slot_assignments(slots), None)

    chosen_by_slot: dict[tuple[ChipType, int], tuple[int, float, uuid.UUID | None]] = {}
    for c in candidates:
        if x[c.candidate_id].value() == 1:
            chosen_by_slot[(c.chip_type, c.window_number)] = (c.fpl_event_id, c.gain_mean, c.candidate_id)
    for variant in variants:
        if v[variant.variant_id].value() == 1:
            chosen_by_slot[(variant.chip_type, variant.window_number)] = (
                variant.fpl_event_id,
                variant.gain_mean,
                None,
            )

    assignments: list[ChipScheduleAssignmentResult] = []
    total_expected_gain = 0.0
    for chip_type, window_number in slots:
        chosen = chosen_by_slot.get((chip_type, window_number))
        if chosen is None:
            assignments.append(ChipScheduleAssignmentResult(chip_type, window_number, None, None, None))
            continue
        fpl_event_id, gain_mean, source_candidate_id = chosen
        assignments.append(
            ChipScheduleAssignmentResult(
                chip_type, window_number, fpl_event_id, gain_mean, source_candidate_id
            )
        )
        total_expected_gain += gain_mean

    return ChipScheduleSolution(status, assignments, total_expected_gain)
