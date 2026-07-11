"""SQUAD_OPTIMIZATION job: jointly solve the 15-player squad, starting XI, and captain/vice.

Refuses outright (no ``optimization_runs`` row at all) when
``app.optimization.gate.check_optimization_gate`` blocks. Otherwise creates a
row up front and keeps it in sync with the run's outcome — including the
INFEASIBLE case, which is a real, informative outcome worth persisting, not
a "never ran" state.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.jobs.registry import register_handler
from app.jobs.runner import JobContext, JobResult, JobRunner
from app.optimization.candidates import OptimizationCandidate
from app.optimization.constraints import get_captain_multiplier, load_optimization_constraints
from app.optimization.gameweek_resolution import resolve_target_gameweek
from app.optimization.gate import check_optimization_gate
from app.optimization.joint_squad_solver import solve_joint_squad_and_xi
from app.optimization.persistence import build_run_player_rows_and_xi_points
from app.optimization.projection import PROJECTION_FORMULA_VERSION, project_player
from packages.database.models.optimization_run import OptimizationRun
from packages.database.models.player import Player
from packages.database.models.position import Position
from packages.database.models.season import Season
from packages.database.session import SessionLocal
from packages.domain.jobs import JobStatus, JobType
from packages.domain.optimization import OptimizationRunStatus, RecommendationType, SolverStatus


def _resolve_current_season_id(session: Session) -> uuid.UUID | None:
    return session.execute(select(Season.id).where(Season.is_current.is_(True))).scalar_one_or_none()


def _build_candidates(session: Session, season_id: uuid.UUID) -> list[OptimizationCandidate]:
    """Build the eligible candidate pool: non-quarantined players with a resolved position/team."""
    players = (
        session.execute(select(Player).where(Player.season_id == season_id, Player.is_quarantined.is_(False)))
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
        if not projection.is_eligible:
            continue
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


@register_handler(JobType.SQUAD_OPTIMIZATION)
class SquadOptimizationJobRunner(JobRunner):
    """Solves for the 15-player squad + starting XI + captain/vice jointly.

    Uses ``app.optimization.joint_squad_solver.solve_joint_squad_and_xi`` (a
    two-phase lexicographic MIP: maximize XI+captain points, then minimize
    bench cost) rather than the old two-stage squad-then-lineup approach,
    which had no incentive to keep bench players cheap. Produces a
    JOINT_SQUAD_AND_XI recommendation with the full squad, starting XI,
    ordered bench, and captain/vice already resolved in one run.

    Wraps its own orchestration in a try/except so the ``optimization_runs``
    row always reaches a terminal status, matching the pattern established by
    ``DataIngestionJobRunner``/``RulesSyncJobRunner``.
    """

    def run(self, ctx: JobContext) -> JobResult:
        session = SessionLocal()
        run_row: OptimizationRun | None = None
        try:
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
                captain_multiplier = get_captain_multiplier(session, constraints.rules_config_id)
                target_gameweek_id, target_gameweek_status = resolve_target_gameweek(session, season_id)

                run_row = OptimizationRun(
                    job_id=uuid.UUID(ctx.job_id),
                    season_id=season_id,
                    rules_config_id=constraints.rules_config_id,
                    recommendation_type=RecommendationType.JOINT_SQUAD_AND_XI,
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

                candidates = _build_candidates(session, season_id)
                solution = solve_joint_squad_and_xi(candidates, constraints, captain_multiplier)

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
                        error_code="SQUAD_OPTIMIZATION_INFEASIBLE",
                        error_message="No feasible 15-player squad could be solved under the current "
                        "constraints and candidate pool.",
                    )

                # solve_joint_squad_and_xi only returns solver_status=OPTIMAL with both
                # captain fields set — the asserts below just narrow the type for mypy.
                assert solution.captain_player_id is not None
                assert solution.vice_captain_player_id is not None

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
                run_row.squad_player_ids = [str(pid) for pid in solution.squad_player_ids]
                run_row.starting_xi_player_ids = [str(pid) for pid in solution.starting_player_ids]
                run_row.bench_player_ids_ordered = [str(pid) for pid in solution.bench_player_ids_ordered]
                run_row.captain_player_id = solution.captain_player_id
                run_row.vice_captain_player_id = solution.vice_captain_player_id
                run_row.squad_total_cost = solution.squad_total_cost
                run_row.squad_projected_points = (
                    Decimal(str(solution.squad_projected_points))
                    if solution.squad_projected_points is not None
                    else None
                )
                run_row.xi_projected_points = Decimal(str(xi_projected_points))
                run_row.completed_at = datetime.now(UTC)
                session.commit()

                return JobResult(
                    status=JobStatus.SUCCEEDED,
                    output={
                        "optimization_run_id": str(run_row.id),
                        "squad_player_ids": run_row.squad_player_ids,
                        "squad_total_cost": run_row.squad_total_cost,
                        "squad_projected_points": float(run_row.squad_projected_points)
                        if run_row.squad_projected_points is not None
                        else None,
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
                    error_code="SQUAD_OPTIMIZATION_FAILED",
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
