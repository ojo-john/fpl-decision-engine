"""CHIP_SCHEDULE_OPTIMIZATION job (Phase 1 of the joint chip scheduler): given an existing,
already-SUCCEEDED ``CHIP_SIMULATION`` run's own persisted ``chip_window_candidates``, pick the
best non-colliding gameweek-to-chip assignment across all 4 chips.

Real incident this fixes: ``CHIP_SIMULATION`` evaluates each of the 4 FPL chips (Wildcard,
Free Hit, Bench Boost, Triple Captain) completely independently — confirmed live: it
simultaneously recommended "use now" for Free Hit, Bench Boost, AND Triple Captain, all for the
SAME gameweek. A real FPL manager can only play one chip per gameweek, so 3 independent "use now"
verdicts for one week is not an actionable recommendation, it's 3 contradictory ones. Nowhere in
this codebase was that limitation previously documented.

This handler does NOT re-evaluate anything — every gain it works with is read verbatim from the
source run's own ``chip_window_candidates`` rows, exactly as ``app.chips.scenario`` already
computed them. The only new work is the assignment/scheduling layer itself
(``app.chips.schedule_solver.solve_chip_schedule``, a small PuLP MIP — see that module's own
docstring for the exact constraints). This is a deliberate, stated Phase 1 scope boundary:
compounding effects between chips (e.g. a Wildcard rebuild changing what Bench Boost is worth in a
gameweek after it) are NOT modeled here — every input gain is treated as fixed and independent,
exactly as it already was before this job existed. That deeper, path-dependent recomputation,
across BOTH windows for every chip (not just whichever window ``CHIP_SIMULATION`` currently has
open), is Phase 2 — a real, larger, separate piece of work, not silently bundled into this one.

No optimization-gate check here (mirrors ``RECOMMENDATION_GENERATION``'s identical precedent,
see that handler's own module docstring): this job runs no fresh optimization or projection of
its own, it only consumes an already-gated run's persisted output — the gate was already checked
when the source ``CHIP_SIMULATION`` run itself was created.

Input resolution (``parameters.chip_optimization_run_id``, optional): an explicit run id is used
as-is (must exist and be SUCCEEDED); omitted resolves the most recently completed SUCCEEDED
``CHIP_SIMULATION`` run across the whole system (mirrors this codebase's own established
"auto-resolve if not given" convention, e.g. ``RECOMMENDATION_GENERATION``'s target-gameweek
resolution) — there is at most one real manager configured in this system today (see
``app.jobs.handlers.recommendation_generation``'s own docstring for the same real, stated
single-manager assumption), so "most recent" is unambiguous.

Zero eligible candidates (e.g. every chip already used, or the source run evaluated none) is a
valid SUCCEEDED outcome with an empty assignment set — never a refusal; there is nothing wrong
with "nothing left to schedule."

Create-early-RUNNING-row discipline mirrors ``ChipSimulationJobRunner``'s own documented
convention: input/source-run resolution happens BEFORE the ``chip_schedule_optimization_runs`` row
is created (a hard refusal on a missing/unusable source run never pollutes the audit trail); the
row is created immediately after, before the solve itself, so a crash mid-run still leaves a
terminal FAILED row via ``_finalize_failed``.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.chips.schedule_solver import ChipScheduleCandidateInput, solve_chip_schedule
from app.jobs.registry import register_handler
from app.jobs.runner import JobContext, JobResult, JobRunner
from packages.database.models.chip_optimization_run import ChipOptimizationRun
from packages.database.models.chip_schedule_assignment import ChipScheduleAssignment
from packages.database.models.chip_schedule_optimization_run import ChipScheduleOptimizationRun
from packages.database.models.chip_window_candidate import ChipWindowCandidate
from packages.database.session import SessionLocal
from packages.domain.chips import ChipOptimizationRunStatus, ChipScheduleRunStatus
from packages.domain.jobs import JobStatus, JobType
from packages.domain.optimization import SolverStatus


class _InputResolutionError(Exception):
    """Raised for a clear, user-facing input problem, always caught before any row is created."""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


def _resolve_source_run(session: Session, parameters: dict | None) -> ChipOptimizationRun:
    raw = (parameters or {}).get("chip_optimization_run_id")
    if raw is not None:
        try:
            run_id = uuid.UUID(str(raw))
        except (ValueError, AttributeError, TypeError):
            raise _InputResolutionError(
                "INVALID_CHIP_OPTIMIZATION_RUN_ID", "parameters.chip_optimization_run_id is not a valid UUID."
            ) from None
        run = session.get(ChipOptimizationRun, run_id)
        if run is None:
            raise _InputResolutionError(
                "CHIP_OPTIMIZATION_RUN_NOT_FOUND", f"chip_optimization_runs row {run_id} not found."
            )
        if run.run_status != ChipOptimizationRunStatus.SUCCEEDED:
            raise _InputResolutionError(
                "CHIP_OPTIMIZATION_RUN_NOT_SUCCEEDED",
                f"chip_optimization_runs row {run_id} has run_status={run.run_status.value}, not succeeded "
                "-- nothing usable to schedule from.",
            )
        return run

    run = session.execute(
        select(ChipOptimizationRun)
        .where(ChipOptimizationRun.run_status == ChipOptimizationRunStatus.SUCCEEDED)
        .order_by(ChipOptimizationRun.completed_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if run is None:
        raise _InputResolutionError(
            "NO_SUCCEEDED_CHIP_SIMULATION_RUN",
            "No SUCCEEDED CHIP_SIMULATION run exists to schedule from -- run one first.",
        )
    return run


@register_handler(JobType.CHIP_SCHEDULE_OPTIMIZATION)
class ChipScheduleOptimizationJobRunner(JobRunner):
    """Picks the best non-colliding gameweek-to-chip assignment from an existing CHIP_SIMULATION
    run's own already-computed candidates. See module docstring for the full design."""

    def run(self, ctx: JobContext) -> JobResult:
        session = SessionLocal()
        run_row: ChipScheduleOptimizationRun | None = None
        try:
            try:
                source_run = _resolve_source_run(session, ctx.parameters)
            except _InputResolutionError as exc:
                return JobResult(
                    status=JobStatus.FAILED, output=None, error_code=exc.error_code, error_message=str(exc)
                )

            try:
                started_at = datetime.now(UTC)
                run_row = ChipScheduleOptimizationRun(
                    job_id=uuid.UUID(ctx.job_id),
                    source_chip_optimization_run_id=source_run.id,
                    manager_squad_snapshot_id=source_run.manager_squad_snapshot_id,
                    season_id=source_run.season_id,
                    run_status=ChipScheduleRunStatus.RUNNING,
                    started_at=started_at,
                )
                session.add(run_row)
                session.commit()
                session.refresh(run_row)

                candidate_rows = (
                    session.execute(
                        select(ChipWindowCandidate).where(
                            ChipWindowCandidate.chip_optimization_run_id == source_run.id
                        )
                    )
                    .scalars()
                    .all()
                )
                candidates = [
                    ChipScheduleCandidateInput(
                        candidate_id=row.id,
                        chip_type=row.chip_type,
                        window_number=row.window_number,
                        fpl_event_id=row.candidate_fpl_event_id,
                        gain_mean=float(row.gain_mean),
                    )
                    for row in candidate_rows
                ]

                solution = solve_chip_schedule(candidates)

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
                            source_chip_window_candidate_id=assignment.source_candidate_id,
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
                        "source_chip_optimization_run_id": str(source_run.id),
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
