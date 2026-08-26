"""CHIP_SCHEDULE_OPTIMIZATION job (the joint chip scheduler): a fresh, full-season evaluation of
all 4 chips, both windows each, picking the best non-colliding gameweek-to-chip assignment.

Real incident this fixes: ``CHIP_SIMULATION`` evaluates each of the 4 FPL chips (Wildcard,
Free Hit, Bench Boost, Triple Captain) completely independently — confirmed live: it
simultaneously recommended "use now" for Free Hit, Bench Boost, AND Triple Captain, all for the
SAME gameweek. A real FPL manager can only play one chip per gameweek, so 3 independent "use now"
verdicts for one week is not an actionable recommendation, it's 3 contradictory ones. Nowhere in
this codebase was that limitation previously documented.

**Phase 1** (shipped first, still the schema this builds on) consumed an existing SUCCEEDED
``CHIP_SIMULATION`` run's already-computed ``chip_window_candidates`` as fixed inputs — fast, but
limited to whichever ONE window per chip that run happened to evaluate, and blind to any
compounding effect between chips.

**This is Phase 2**: a real structural change, not an incremental patch. This handler no longer
reads a prior run's rows at all — it takes a ``manager_squad_snapshot_id`` directly (mirroring
``CHIP_SIMULATION``'s own input shape) and evaluates fresh, via
``app.jobs.handlers.chip_simulation``'s own shared building blocks
(``_resolve_chip_evaluation_inputs``/``_build_chip_evaluation_context``/``_evaluate_chip_candidates``
/``_post_wildcard_evaluation_context`` — imported directly, mirroring this codebase's own existing
precedent of importing ``_build_lineup_candidates`` across handler modules rather than duplicating
real business logic):

1. Resolve inputs and run a REAL optimization-gate check (unlike Phase 1, this job now does its
   own fresh projection/optimization work, so — unlike Phase 1's justified "no gate, reads
   already-gated output" — it needs the same gate every other fresh-evaluation job checks).
2. Evaluate every genuinely ``AVAILABLE`` window (per ``app.chips.inventory
   .derive_user_chip_windows``) for all 4 chips — BOTH windows each, not just whichever one
   ``target_fpl_event_id`` currently falls inside — against the manager's REAL current squad.
   These are the BASELINE candidates, identical in spirit to what ``CHIP_SIMULATION`` itself
   computes for its own one window.
3. **Path dependence**: only Wildcard permanently changes the squad (Free Hit's own squad change
   reverts the following gameweek — never a path-dependence trigger; Bench Boost/Triple Captain
   never change squad composition at all, only apply a transient multiplier). For every Wildcard
   candidate in the season's FIRST occurring (evaluated, available) window, build a hypothetical
   post-rebuild squad context (``_post_wildcard_evaluation_context``, reusing that candidate's own
   already-computed ``JointSquadSolution`` — no re-solving) and re-evaluate every OTHER chip's own
   candidate gameweek that falls chronologically AFTER it, producing a
   ``ChipSchedulePostWildcardVariant`` — an alternate, path-aware gain, only claimable in the final
   assignment if that exact Wildcard candidate is ALSO chosen (see ``app.chips.schedule_solver``'s
   own module docstring for the MIP mechanics).
4. Solve (``app.chips.schedule_solver.solve_chip_schedule``) and persist the winning assignment
   per (chip_type, window_number) slot.

**Stated, deliberate scope boundary**: only the season's first-occurring Wildcard window triggers
path-dependent variants for everything after it — a second Wildcard's own value (if the manager's
first-window Wildcard was also played) is never itself re-evaluated against that hypothetical
rebuild. Modeling that deeper, transitively-chained compounding (Wildcard-after-Wildcard) is real,
separate, larger future work, not built here — the single most impactful real-world case (an
early-season Wildcard reshaping everything that follows) is what this covers. Real, meaningfully
larger compute cost than Phase 1 as a direct consequence: Bench Boost/Triple Captain are evaluated
exhaustively per window (unlike Wildcard/Free Hit, capped at 4 candidates each) at 2000 Monte Carlo
samples per candidate gameweek, and each qualifying Wildcard candidate triggers one additional
re-evaluation pass over every later candidate — bounded (not exponential) but real, likely several
minutes, similar territory to the real ``TRANSFER_OPTIMIZATION`` runs already seen on this
Supabase tier.

``source_chip_optimization_run_id`` (migration ``0048`` made this nullable) is always ``None`` for
a run produced by this handler — there is no longer a source ``CHIP_SIMULATION`` run to point back
to; kept only for historical Phase-1-shaped rows. Likewise ``ChipScheduleAssignment
.source_chip_window_candidate_id`` is always ``None`` here — this handler's own candidates/variants
are ephemeral, computed in-memory, never separately persisted as their own rows (only the winning
assignment is written).

Zero eligible candidates (e.g. every chip already used this season) is a valid SUCCEEDED outcome
with an empty assignment set — never a refusal; there is nothing wrong with "nothing left to
schedule."

Create-early-RUNNING-row discipline mirrors ``ChipSimulationJobRunner``'s own documented
convention: input resolution (including the gate check) happens BEFORE the
``chip_schedule_optimization_runs`` row is created (a hard refusal never pollutes the audit
trail); the row is created immediately after, before the real evaluation work, so a crash mid-run
still leaves a terminal FAILED row via ``_finalize_failed``.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.chips.inventory import derive_user_chip_windows
from app.chips.scenario import ChipWindowCandidateResult
from app.chips.schedule_solver import (
    ChipScheduleCandidateInput,
    ChipSchedulePostWildcardVariant,
    solve_chip_schedule,
)
from app.chips.windows import resolve_chip_windows
from app.jobs.handlers.chip_simulation import (
    _build_chip_evaluation_context,
    _ChipEvaluationContext,
    _ChipEvaluationInputs,
    _evaluate_chip_candidates,
    _InputResolutionError,
    _parse_uuid_parameter,
    _post_wildcard_evaluation_context,
    _resolve_chip_evaluation_inputs,
)
from app.jobs.registry import register_handler
from app.jobs.runner import JobContext, JobResult, JobRunner
from app.optimization.gameweek_resolution import resolve_gameweeks_within_window
from packages.database.models.chip_schedule_assignment import ChipScheduleAssignment
from packages.database.models.chip_schedule_optimization_run import ChipScheduleOptimizationRun
from packages.database.session import SessionLocal
from packages.domain.chips import ChipScheduleRunStatus, ChipType, UserChipWindowStatus
from packages.domain.jobs import JobStatus, JobType
from packages.domain.optimization import SolverStatus


def _evaluate_baseline_candidates(
    session: Session, inputs: _ChipEvaluationInputs, eval_ctx: _ChipEvaluationContext
) -> tuple[
    list[ChipScheduleCandidateInput],
    dict[tuple[ChipType, int], list[ChipWindowCandidateResult]],
    dict[tuple[ChipType, int, int], uuid.UUID],
]:
    """Evaluate every genuinely AVAILABLE window for all 4 chips against the REAL current squad
    (no path dependence yet -- see ``_build_post_wildcard_variants`` below for that). Returns the
    flat candidate list the solver needs, plus the raw per-slot results and a
    (chip_type, window_number, fpl_event_id) -> candidate_id lookup, both needed to build variants
    afterward.
    """
    chip_windows = resolve_chip_windows(inputs.rules_config.raw_chips_json)
    candidates: list[ChipScheduleCandidateInput] = []
    results_by_slot: dict[tuple[ChipType, int], list[ChipWindowCandidateResult]] = {}
    candidate_id_by_key: dict[tuple[ChipType, int, int], uuid.UUID] = {}

    for chip_type in ChipType:
        windows_for_type = [w for w in chip_windows if w.chip_type == chip_type]
        derived = derive_user_chip_windows(
            windows_for_type,
            inputs.snapshot.chips_used_json,
            inputs.snapshot.active_chip,
            inputs.target_fpl_event_id,
        )
        for user_window in derived:
            if user_window.status != UserChipWindowStatus.AVAILABLE:
                continue
            window_gameweek_ids = resolve_gameweeks_within_window(
                session,
                inputs.season_id,
                user_window.window.stop_event,
                eval_ctx.now,
                start_event=user_window.window.start_event,
            )
            if not window_gameweek_ids:
                continue
            results = _evaluate_chip_candidates(session, chip_type, window_gameweek_ids, eval_ctx)
            if not results:
                continue
            window_number = user_window.window.window_number
            results_by_slot[(chip_type, window_number)] = results
            for result in results:
                candidate_id = uuid.uuid4()
                candidate_id_by_key[(chip_type, window_number, result.fpl_event_id)] = candidate_id
                candidates.append(
                    ChipScheduleCandidateInput(
                        candidate_id=candidate_id,
                        chip_type=chip_type,
                        window_number=window_number,
                        fpl_event_id=result.fpl_event_id,
                        gain_mean=result.gain_mean,
                    )
                )

    return candidates, results_by_slot, candidate_id_by_key


def _primary_wildcard_slot(
    results_by_slot: dict[tuple[ChipType, int], list[ChipWindowCandidateResult]],
) -> tuple[int, list[ChipWindowCandidateResult]] | None:
    """The season's FIRST-occurring evaluated Wildcard window (by window_number -- window 1 always
    precedes window 2 in real time) -- see module docstring for why only this one window's own
    candidates trigger path-dependent variants for everything after them. ``None`` if Wildcard has
    no available, evaluated window at all this run (nothing to trigger path dependence from)."""
    wildcard_slots = {
        window_number: results
        for (chip_type, window_number), results in results_by_slot.items()
        if chip_type == ChipType.WILDCARD
    }
    if not wildcard_slots:
        return None
    window_number = min(wildcard_slots)
    return window_number, wildcard_slots[window_number]


def _build_post_wildcard_variants(
    session: Session,
    eval_ctx: _ChipEvaluationContext,
    results_by_slot: dict[tuple[ChipType, int], list[ChipWindowCandidateResult]],
    candidate_id_by_key: dict[tuple[ChipType, int, int], uuid.UUID],
) -> list[ChipSchedulePostWildcardVariant]:
    """For each candidate in the primary Wildcard window, re-evaluate every OTHER chip's own
    already-evaluated LATER candidate gameweeks against the hypothetical post-rebuild squad --
    see module docstring for the full path-dependence design and its stated scope boundary."""
    primary = _primary_wildcard_slot(results_by_slot)
    if primary is None:
        return []
    primary_window_number, wildcard_results = primary

    variants: list[ChipSchedulePostWildcardVariant] = []
    for wildcard_result in wildcard_results:
        wildcard_candidate_id = candidate_id_by_key[
            (ChipType.WILDCARD, primary_window_number, wildcard_result.fpl_event_id)
        ]
        post_ctx = _post_wildcard_evaluation_context(session, eval_ctx, wildcard_result)

        for (chip_type, window_number), results in results_by_slot.items():
            if chip_type == ChipType.WILDCARD and window_number == primary_window_number:
                continue  # can't claim a path-dependent variant of the very trigger itself
            later_gameweek_ids = [
                result.gameweek_id for result in results if result.fpl_event_id > wildcard_result.fpl_event_id
            ]
            if not later_gameweek_ids:
                continue
            variant_results = _evaluate_chip_candidates(session, chip_type, later_gameweek_ids, post_ctx)
            for variant_result in variant_results:
                variants.append(
                    ChipSchedulePostWildcardVariant(
                        variant_id=uuid.uuid4(),
                        chip_type=chip_type,
                        window_number=window_number,
                        fpl_event_id=variant_result.fpl_event_id,
                        gain_mean=variant_result.gain_mean,
                        requires_wildcard_candidate_id=wildcard_candidate_id,
                    )
                )

    return variants


@register_handler(JobType.CHIP_SCHEDULE_OPTIMIZATION)
class ChipScheduleOptimizationJobRunner(JobRunner):
    """Fresh, full-season, path-dependent joint chip schedule. See module docstring for the full
    design."""

    def run(self, ctx: JobContext) -> JobResult:
        session = SessionLocal()
        run_row: ChipScheduleOptimizationRun | None = None
        now = datetime.now(UTC)
        try:
            try:
                manager_squad_snapshot_id = _parse_uuid_parameter(ctx.parameters, "manager_squad_snapshot_id")
                inputs = _resolve_chip_evaluation_inputs(session, manager_squad_snapshot_id, now)
            except _InputResolutionError as exc:
                return JobResult(
                    status=JobStatus.FAILED, output=None, error_code=exc.error_code, error_message=str(exc)
                )

            try:
                started_at = now
                run_row = ChipScheduleOptimizationRun(
                    job_id=uuid.UUID(ctx.job_id),
                    source_chip_optimization_run_id=None,
                    manager_squad_snapshot_id=inputs.snapshot.id,
                    season_id=inputs.season_id,
                    run_status=ChipScheduleRunStatus.RUNNING,
                    started_at=started_at,
                )
                session.add(run_row)
                session.commit()
                session.refresh(run_row)

                eval_ctx = _build_chip_evaluation_context(session, inputs, now)
                candidates, results_by_slot, candidate_id_by_key = _evaluate_baseline_candidates(
                    session, inputs, eval_ctx
                )
                variants = _build_post_wildcard_variants(
                    session, eval_ctx, results_by_slot, candidate_id_by_key
                )

                solution = solve_chip_schedule(candidates, variants=variants)

                if solution.solver_status != SolverStatus.OPTIMAL:
                    run_row.run_status = ChipScheduleRunStatus.FAILED
                    run_row.solver_status = solution.solver_status
                    run_row.completed_at = datetime.now(UTC)
                    session.commit()
                    return JobResult(
                        status=JobStatus.FAILED,
                        output={"chip_schedule_optimization_run_id": str(run_row.id)},
                        error_code="CHIP_SCHEDULE_INFEASIBLE",
                        error_message="No feasible chip-to-gameweek assignment could be solved.",
                    )

                for assignment in solution.assignments:
                    session.add(
                        ChipScheduleAssignment(
                            chip_schedule_optimization_run_id=run_row.id,
                            chip_type=assignment.chip_type,
                            window_number=assignment.window_number,
                            assigned_fpl_event_id=assignment.assigned_fpl_event_id,
                            gain_mean=(
                                Decimal(str(assignment.gain_mean))
                                if assignment.gain_mean is not None
                                else None
                            ),
                            # Always None here -- this handler's own candidates/variants are
                            # ephemeral, never persisted as their own chip_window_candidates rows
                            # (see module docstring).
                            source_chip_window_candidate_id=None,
                        )
                    )

                run_row.run_status = ChipScheduleRunStatus.SUCCEEDED
                run_row.solver_status = SolverStatus.OPTIMAL
                run_row.total_expected_gain = (
                    Decimal(str(solution.total_expected_gain))
                    if solution.total_expected_gain is not None
                    else None
                )
                run_row.completed_at = datetime.now(UTC)
                session.commit()

                return JobResult(
                    status=JobStatus.SUCCEEDED,
                    output={
                        "chip_schedule_optimization_run_id": str(run_row.id),
                        "manager_squad_snapshot_id": str(inputs.snapshot.id),
                        "total_expected_gain": (
                            float(solution.total_expected_gain)
                            if solution.total_expected_gain is not None
                            else None
                        ),
                    },
                    error_code=None,
                    error_message=None,
                )
            except Exception as exc:  # noqa: BLE001 - the run row must always reach a terminal status
                session.rollback()
                self._finalize_failed(session, run_row)
                failed_output = (
                    {"chip_schedule_optimization_run_id": str(run_row.id)} if run_row is not None else None
                )
                return JobResult(
                    status=JobStatus.FAILED,
                    output=failed_output,
                    error_code="CHIP_SCHEDULE_OPTIMIZATION_FAILED",
                    error_message=str(exc),
                )
        finally:
            session.close()

    def _finalize_failed(self, session: Session, run_row: ChipScheduleOptimizationRun | None) -> None:
        if run_row is None or run_row.id is None:
            return
        try:
            fresh = session.get(ChipScheduleOptimizationRun, run_row.id)
            if fresh is not None and fresh.run_status == ChipScheduleRunStatus.RUNNING:
                fresh.run_status = ChipScheduleRunStatus.FAILED
                fresh.completed_at = datetime.now(UTC)
                session.commit()
        except Exception:  # noqa: BLE001 - best-effort finalization only
            session.rollback()
