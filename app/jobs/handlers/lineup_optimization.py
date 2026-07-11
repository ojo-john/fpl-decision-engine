"""LINEUP_OPTIMIZATION job: pick the starting XI (+bench, captain, vice) from a fixed 15-player squad.

Input squad source is exactly one of:
- ``{"source_optimization_run_id": "<uuid>"}`` — a prior SUCCEEDED
  INITIAL_SQUAD or JOINT_SQUAD_AND_XI run's ``squad_player_ids`` (both
  produce a valid 15-player squad to chain a fresh lineup solve from).
- ``{"player_ids": [<uuid>, ... x15]}`` — an explicit squad, for future
  manager-squad-sync use (Milestone 4).

Unlike ``SquadOptimizationJobRunner``'s candidate pool, every one of the 15
squad players is included as a lineup candidate regardless of
``PlayerProjection.is_eligible`` — a squad member who has since become
injured must still be considered (and benched, not vanished) here; only
their *projected_points* drops to 0, which naturally deters the solver from
starting them without making the problem infeasible.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.jobs.registry import register_handler
from app.jobs.runner import JobContext, JobResult, JobRunner
from app.optimization.candidates import OptimizationCandidate
from app.optimization.constraints import get_captain_multiplier, load_optimization_constraints
from app.optimization.gameweek_resolution import resolve_target_gameweek
from app.optimization.gate import check_optimization_gate
from app.optimization.lineup_solver import solve_lineup
from app.optimization.persistence import build_run_player_rows_and_xi_points
from app.optimization.projection import PROJECTION_FORMULA_VERSION, project_player
from packages.database.models.optimization_run import OptimizationRun
from packages.database.models.player import Player
from packages.database.models.position import Position
from packages.database.models.season import Season
from packages.database.session import SessionLocal
from packages.domain.jobs import JobStatus, JobType
from packages.domain.optimization import OptimizationRunStatus, RecommendationType, SolverStatus

SQUAD_SIZE = 15

# A LINEUP_OPTIMIZATION run may chain off any prior run type that produced a
# valid, fixed 15-player squad — both INITIAL_SQUAD (historical, pre-Fix-2
# rows) and JOINT_SQUAD_AND_XI (current squad-producing run type) qualify.
_VALID_SQUAD_SOURCE_RECOMMENDATION_TYPES = {
    RecommendationType.INITIAL_SQUAD,
    RecommendationType.JOINT_SQUAD_AND_XI,
}


def _resolve_current_season_id(session: Session) -> uuid.UUID | None:
    return session.execute(select(Season.id).where(Season.is_current.is_(True))).scalar_one_or_none()


class InvalidLineupInputError(Exception):
    """Raised when ``ctx.parameters`` doesn't supply exactly one valid squad source."""


def _parse_lineup_input(
    session: Session, parameters: dict[str, Any] | None
) -> tuple[list[uuid.UUID], uuid.UUID | None]:
    """Validate ``ctx.parameters`` and resolve it to a concrete list of 15 player ids.

    Returns ``(player_ids, source_optimization_run_id)``. Raises
    ``InvalidLineupInputError`` for any malformed/missing/ambiguous input, or
    if a referenced source run doesn't exist / isn't a SUCCEEDED INITIAL_SQUAD.
    """
    parameters = parameters or {}
    has_source = parameters.get("source_optimization_run_id") is not None
    has_player_ids = parameters.get("player_ids") is not None

    if has_source == has_player_ids:
        raise InvalidLineupInputError(
            "Exactly one of 'source_optimization_run_id' or 'player_ids' must be provided."
        )

    if has_source:
        try:
            source_run_id = uuid.UUID(str(parameters["source_optimization_run_id"]))
        except (ValueError, AttributeError, TypeError) as exc:
            raise InvalidLineupInputError("'source_optimization_run_id' is not a valid UUID.") from exc

        source_run = session.get(OptimizationRun, source_run_id)
        if source_run is None:
            raise InvalidLineupInputError(f"optimization_runs row {source_run_id} not found.")
        if source_run.recommendation_type not in _VALID_SQUAD_SOURCE_RECOMMENDATION_TYPES:
            raise InvalidLineupInputError(
                f"optimization_runs row {source_run_id} is not a squad-producing run "
                f"(must be one of {sorted(t.value for t in _VALID_SQUAD_SOURCE_RECOMMENDATION_TYPES)})."
            )
        if source_run.run_status != OptimizationRunStatus.SUCCEEDED:
            raise InvalidLineupInputError(f"optimization_runs row {source_run_id} did not SUCCEED.")
        if not source_run.squad_player_ids or len(source_run.squad_player_ids) != SQUAD_SIZE:
            raise InvalidLineupInputError(
                f"optimization_runs row {source_run_id} has no valid squad_player_ids."
            )

        try:
            player_ids = [uuid.UUID(str(pid)) for pid in source_run.squad_player_ids]
        except (ValueError, AttributeError, TypeError) as exc:
            raise InvalidLineupInputError("source run's squad_player_ids contains an invalid UUID.") from exc
        return player_ids, source_run_id

    raw_ids = parameters["player_ids"]
    if not isinstance(raw_ids, list) or len(raw_ids) != SQUAD_SIZE:
        raise InvalidLineupInputError(f"'player_ids' must be a list of exactly {SQUAD_SIZE} UUIDs.")
    try:
        player_ids = [uuid.UUID(str(pid)) for pid in raw_ids]
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidLineupInputError("'player_ids' contains an invalid UUID.") from exc
    return player_ids, None


def _build_lineup_candidates(
    session: Session, season_id: uuid.UUID, player_ids: list[uuid.UUID]
) -> list[OptimizationCandidate]:
    """Build one candidate per requested player id that actually resolves in this season.

    No eligibility filter here (see module docstring) — every resolvable
    squad member is included, with ``projected_points=0.0`` for hard-excluded
    statuses, so the lineup solver can still legally bench them.
    """
    players = (
        session.execute(select(Player).where(Player.season_id == season_id, Player.id.in_(player_ids)))
        .scalars()
        .all()
    )
    positions_by_id = {
        p.id: p for p in session.execute(select(Position).where(Position.season_id == season_id)).scalars()
    }

    candidates = []
    for player in players:
        if player.team_id is None or player.position_id is None:
            continue
        position = positions_by_id.get(player.position_id)
        if position is None:
            continue
        projection = project_player(player)
        candidates.append(
            OptimizationCandidate(
                player_id=player.id,
                projected_points=projection.projected_points,
                projection_source=projection.source,
                position_short_name=position.short_name,
                team_id=player.team_id,
                now_cost=player.now_cost,
            )
        )
    return candidates


@register_handler(JobType.LINEUP_OPTIMIZATION)
class LineupOptimizationJobRunner(JobRunner):
    """Solves for the starting XI (+ordered bench, captain, vice) from a fixed 15-player squad."""

    def run(self, ctx: JobContext) -> JobResult:
        session = SessionLocal()
        run_row: OptimizationRun | None = None
        try:
            try:
                player_ids, source_run_id = _parse_lineup_input(session, ctx.parameters)
            except InvalidLineupInputError as exc:
                return JobResult(
                    status=JobStatus.FAILED,
                    output=None,
                    error_code="INVALID_LINEUP_INPUT",
                    error_message=str(exc),
                )

            season_id = _resolve_current_season_id(session)
            if season_id is None:
                return JobResult(
                    status=JobStatus.FAILED,
                    output=None,
                    error_code="NO_CURRENT_SEASON",
                    error_message="No current season found.",
                )

            gate = check_optimization_gate(session, season_id)
            if not gate.allowed:
                return JobResult(
                    status=JobStatus.FAILED,
                    output=None,
                    error_code=gate.reason_code,
                    error_message=f"Optimization gate refused: {gate.reason_code}",
                )

            try:
                started_at = datetime.now(UTC)
                constraints = load_optimization_constraints(session, season_id)
                target_gameweek_id, target_gameweek_status = resolve_target_gameweek(session, season_id)

                run_row = OptimizationRun(
                    job_id=uuid.UUID(ctx.job_id),
                    season_id=season_id,
                    rules_config_id=constraints.rules_config_id,
                    recommendation_type=RecommendationType.STARTING_XI,
                    source_optimization_run_id=source_run_id,
                    run_status=OptimizationRunStatus.RUNNING,
                    target_gameweek_id=target_gameweek_id,
                    target_gameweek_status=target_gameweek_status,
                    optimization_permitted_snapshot=gate.optimization_permitted,
                    data_quality_score_snapshot=(
                        Decimal(str(gate.data_quality_score)) if gate.data_quality_score is not None else None
                    ),
                    rules_status_snapshot=gate.rules_status,
                    projection_formula_version=PROJECTION_FORMULA_VERSION,
                    parameters_json=ctx.parameters,
                    started_at=started_at,
                )
                session.add(run_row)
                session.commit()
                session.refresh(run_row)

                candidates = _build_lineup_candidates(session, season_id, player_ids)
                solution = solve_lineup(candidates, constraints.position_requirements)

                if solution.solver_status != SolverStatus.OPTIMAL:
                    run_row.run_status = OptimizationRunStatus.FAILED
                    run_row.solver_status = solution.solver_status
                    run_row.completed_at = datetime.now(UTC)
                    session.commit()
                    return JobResult(
                        status=JobStatus.FAILED,
                        output={
                            "optimization_run_id": str(run_row.id),
                            "target_gameweek_status": target_gameweek_status.value,
                        },
                        error_code="LINEUP_OPTIMIZATION_INFEASIBLE",
                        error_message="No feasible starting XI could be solved from the given squad under "
                        "the current constraints.",
                    )

                # solve_lineup only returns solver_status=OPTIMAL with both captain fields set —
                # the asserts below just narrow the type for mypy, they can never actually fire.
                assert solution.captain_player_id is not None
                assert solution.vice_captain_player_id is not None

                captain_multiplier = get_captain_multiplier(session, constraints.rules_config_id)
                candidates_by_id = {c.player_id: c for c in candidates}
                run_player_rows, xi_projected_points = build_run_player_rows_and_xi_points(
                    optimization_run_id=run_row.id,
                    candidates_by_id=candidates_by_id,
                    starting_player_ids=solution.starting_player_ids,
                    bench_player_ids_ordered=solution.bench_player_ids_ordered,
                    captain_player_id=solution.captain_player_id,
                    vice_captain_player_id=solution.vice_captain_player_id,
                    captain_multiplier=captain_multiplier,
                )
                session.add_all(run_player_rows)

                run_row.run_status = OptimizationRunStatus.SUCCEEDED
                run_row.solver_status = SolverStatus.OPTIMAL
                run_row.squad_player_ids = [str(pid) for pid in player_ids]
                run_row.starting_xi_player_ids = [str(pid) for pid in solution.starting_player_ids]
                run_row.bench_player_ids_ordered = [str(pid) for pid in solution.bench_player_ids_ordered]
                run_row.captain_player_id = solution.captain_player_id
                run_row.vice_captain_player_id = solution.vice_captain_player_id
                run_row.squad_total_cost = sum(c.now_cost for c in candidates)
                run_row.squad_projected_points = Decimal(str(sum(c.projected_points for c in candidates)))
                run_row.xi_projected_points = Decimal(str(xi_projected_points))
                run_row.completed_at = datetime.now(UTC)
                session.commit()

                return JobResult(
                    status=JobStatus.SUCCEEDED,
                    output={
                        "optimization_run_id": str(run_row.id),
                        "starting_xi_player_ids": run_row.starting_xi_player_ids,
                        "bench_player_ids_ordered": run_row.bench_player_ids_ordered,
                        "captain_player_id": str(run_row.captain_player_id),
                        "vice_captain_player_id": str(run_row.vice_captain_player_id),
                        "xi_projected_points": float(run_row.xi_projected_points),
                        "target_gameweek_status": target_gameweek_status.value,
                    },
                    error_code=None,
                    error_message=None,
                )
            except Exception as exc:  # noqa: BLE001 - optimization_runs row must always reach a terminal status
                session.rollback()
                self._finalize_failed(session, run_row)
                return JobResult(
                    status=JobStatus.FAILED,
                    output={"optimization_run_id": str(run_row.id)} if run_row is not None else None,
                    error_code="LINEUP_OPTIMIZATION_FAILED",
                    error_message=str(exc),
                )
        finally:
            session.close()

    def _finalize_failed(self, session: Session, run_row: OptimizationRun | None) -> None:
        if run_row is None or run_row.id is None:
            return
        try:
            fresh = session.get(OptimizationRun, run_row.id)
            if fresh is not None and fresh.run_status == OptimizationRunStatus.RUNNING:
                fresh.run_status = OptimizationRunStatus.FAILED
                fresh.completed_at = datetime.now(UTC)
                session.commit()
        except Exception:  # noqa: BLE001 - best-effort finalization only
            session.rollback()
