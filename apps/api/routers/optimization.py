"""POST /optimize/* endpoints.

Uses the request's own DB session for the outer ``Job`` bookkeeping via
``execute_job`` — the job handler internally opens its own session for
``optimization_runs``, exactly like ``DataIngestionJobRunner`` already does
(see ``app/jobs/handlers/data_ingestion.py``); this is an established
pattern, not a new one.
"""

import uuid

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app.jobs.handlers.squad_optimization import _build_candidates
from app.jobs.service import execute_job
from app.optimization.champion_loader import load_current_champion
from app.optimization.constraints import (
    RulesBlockedError,
    get_captain_multiplier,
    load_optimization_constraints,
)
from app.optimization.gameweek_resolution import resolve_target_gameweek
from app.optimization.joint_squad_solver import solve_joint_squad_and_xi
from apps.api.dependencies import get_db
from apps.api.schemas.optimization import (
    InitialSquadRequest,
    LineupRequest,
    OptimizationRunOut,
    OptimizationRunPlayerOut,
    OptimizationRunSummaryOut,
    SquadPreviewOut,
    TransferMoveOut,
    TransferOptimizationRequest,
    TransferOptimizationRunOut,
    TransferOptimizationRunSummaryOut,
    TransferScenarioOut,
)
from packages.database.models.gameweek import Gameweek
from packages.database.models.optimization_run import OptimizationRun
from packages.database.models.optimization_run_player import OptimizationRunPlayer
from packages.database.models.player import Player
from packages.database.models.player_gameweek_stats import PlayerGameweekStats
from packages.database.models.position import Position
from packages.database.models.season import Season
from packages.database.models.transfer_optimization_run import TransferOptimizationRun
from packages.database.models.transfer_optimization_run_move import TransferOptimizationRunMove
from packages.database.models.transfer_optimization_run_scenario import TransferOptimizationRunScenario
from packages.domain.jobs import JobStatus, JobType, TriggerType
from packages.domain.optimization import TargetGameweekStatus

_RECOMMENDED_ACTION_BY_TRANSFERS_COUNT = {0: "roll", 1: "one_transfer", 2: "two_transfers"}

# Same "capped read, not full pagination" convention as apps.api.routers.managers/learning/
# leagues -- this system's own run volume (one manager, on-demand triggers) never approaches
# a size where offset/limit paging is actually needed.
_MAX_RESULTS = 200

router = APIRouter(prefix="/optimize", tags=["optimization"])


def _error_response(job) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "error_code": job.error_code,
            "error_message": job.error_message,
            "job_id": str(job.id),
        },
    )


def _not_found_response(model_name: str, run_id: uuid.UUID) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={"error_code": f"{model_name}_NOT_FOUND", "detail": f"{model_name} {run_id} not found."},
    )


def _load_run_response(db: Session, optimization_run_id: uuid.UUID) -> OptimizationRunOut:
    run = db.get(OptimizationRun, optimization_run_id)
    assert run is not None  # a SUCCEEDED job always leaves its optimization_runs row behind

    rows = db.execute(
        select(OptimizationRunPlayer, Player, Position)
        .join(Player, OptimizationRunPlayer.player_id == Player.id)
        .outerjoin(Position, Player.position_id == Position.id)
        .where(OptimizationRunPlayer.optimization_run_id == optimization_run_id)
    ).all()

    players_out = [
        OptimizationRunPlayerOut(
            player_id=run_player.player_id,
            web_name=player.web_name,
            position=position.short_name if position is not None else None,
            team_id=player.team_id,
            is_starting=run_player.is_starting,
            bench_order=run_player.bench_order,
            is_captain=run_player.is_captain,
            is_vice_captain=run_player.is_vice_captain,
            projected_points=run_player.projected_points,
            projection_source=run_player.projection_source,
            now_cost_snapshot=run_player.now_cost_snapshot,
        )
        for run_player, player, position in rows
    ]

    return OptimizationRunOut(
        optimization_run_id=run.id,
        recommendation_type=run.recommendation_type.value,
        run_status=run.run_status.value,
        solver_status=run.solver_status.value if run.solver_status is not None else None,
        target_gameweek_status=run.target_gameweek_status.value,
        squad_total_cost=run.squad_total_cost,
        squad_projected_points=run.squad_projected_points,
        xi_projected_points=run.xi_projected_points,
        captain_player_id=run.captain_player_id,
        vice_captain_player_id=run.vice_captain_player_id,
        players=players_out,
        started_at=run.started_at,
        completed_at=run.completed_at,
    )


@router.post("/initial-squad", response_model=OptimizationRunOut)
def optimize_initial_squad(
    body: InitialSquadRequest, db: Session = Depends(get_db)
) -> OptimizationRunOut | JSONResponse:
    job = execute_job(
        db,
        job_type=JobType.SQUAD_OPTIMIZATION,
        requested_by=body.requested_by,
        trigger_type=TriggerType.API,
    )
    if job.status != JobStatus.SUCCEEDED:
        return _error_response(job)

    assert job.output_json is not None  # a SUCCEEDED job always has output_json
    optimization_run_id = uuid.UUID(job.output_json["optimization_run_id"])
    return _load_run_response(db, optimization_run_id)


@router.post("/preview", response_model=SquadPreviewOut)
def preview_squad(db: Session = Depends(get_db)) -> SquadPreviewOut | JSONResponse:
    """Unofficial, throwaway squad preview -- deliberately bypasses
    ``app.optimization.gate.check_optimization_gate`` entirely (never calls it, never touches
    ``execute_job``/``Job``/``optimization_runs``) so a manager can see "what would this
    recommend" during a window the real gate refuses (most commonly: pre-season, before FPL
    marks a gameweek ``is_current``). Nothing is persisted -- rerunning this produces a fresh
    computation every time, never a cached/stored result to revisit later.

    ``is_cold_start_gameweek`` tells the caller this is the season's own first gameweek --
    see ``app.optimization.projection._is_cold_start_season``/``_cold_start_projection`` for
    why every player's projection source becomes ``cold_start_blend_ep_next_and_career_avg``
    in that state rather than the champion model directly (a real, discovered failure mode:
    the trained model's features are almost entirely rolling within-season form, uniformly
    blank at literal kickoff -- an input shape it was never meaningfully trained on -- so its
    raw predictions were systematically far too low; confirmed live: 0.43 points for Haaland
    versus a sane ~5 once blended with his real prior-season average). The caller should still
    surface this flag (it's informational -- "no current-season signal yet" -- not a data-
    quality warning anymore now that the blend fixes the magnitude problem).
    """
    season_id = db.execute(select(Season.id).where(Season.is_current.is_(True))).scalar_one_or_none()
    if season_id is None:
        return JSONResponse(
            status_code=404, content={"error_code": "NO_CURRENT_SEASON", "detail": "No current season found."}
        )

    target_gameweek_id, status = resolve_target_gameweek(db, season_id)
    if status == TargetGameweekStatus.NO_ACTIVE_GAMEWEEK:
        return JSONResponse(
            status_code=409,
            content={
                "error_code": "NO_ACTIVE_GAMEWEEK",
                "detail": "No unfinished gameweek exists to preview a squad against (off-season).",
            },
        )
    assert target_gameweek_id is not None
    target_gameweek = db.get(Gameweek, target_gameweek_id)
    assert target_gameweek is not None

    try:
        constraints = load_optimization_constraints(db, season_id)
    except RulesBlockedError as exc:
        return JSONResponse(status_code=409, content={"error_code": "RULES_BLOCKED", "detail": str(exc)})

    is_cold_start_gameweek = (
        db.execute(
            select(func.count())
            .select_from(PlayerGameweekStats)
            .where(PlayerGameweekStats.season_id == season_id)
        ).scalar_one()
        == 0
    )

    champion = load_current_champion(db)
    captain_multiplier = get_captain_multiplier(db, constraints.rules_config_id)
    candidates = _build_candidates(db, season_id, target_gameweek_id, champion)
    solution = solve_joint_squad_and_xi(candidates, constraints, captain_multiplier)

    candidates_by_id = {c.player_id: c for c in candidates}
    squad_player_ids = solution.starting_player_ids + solution.bench_player_ids_ordered
    players_by_id = {
        p.id: p for p in db.execute(select(Player).where(Player.id.in_(squad_player_ids))).scalars()
    }
    positions_by_id = {
        p.id: p for p in db.execute(select(Position).where(Position.season_id == season_id)).scalars()
    }

    starter_id_set = set(solution.starting_player_ids)
    bench_order_by_id = {pid: 12 + index for index, pid in enumerate(solution.bench_player_ids_ordered)}

    players_out = []
    for pid in squad_player_ids:
        player = players_by_id.get(pid)
        if player is None:
            continue
        position = positions_by_id.get(player.position_id) if player.position_id is not None else None
        players_out.append(
            OptimizationRunPlayerOut(
                player_id=pid,
                web_name=player.web_name,
                position=position.short_name if position is not None else None,
                team_id=player.team_id,
                is_starting=pid in starter_id_set,
                bench_order=bench_order_by_id.get(pid),
                is_captain=pid == solution.captain_player_id,
                is_vice_captain=pid == solution.vice_captain_player_id,
                projected_points=candidates_by_id[pid].projected_points,
                projection_source=candidates_by_id[pid].projection_source,
                now_cost_snapshot=player.now_cost,
            )
        )

    return SquadPreviewOut(
        solver_status=solution.solver_status.value,
        target_fpl_event_id=target_gameweek.fpl_event_id,
        is_cold_start_gameweek=is_cold_start_gameweek,
        champion_model_family=champion.model_family.value if champion is not None else None,
        squad_total_cost=solution.squad_total_cost,
        squad_projected_points=solution.squad_projected_points,
        xi_projected_points=solution.xi_projected_points,
        captain_player_id=solution.captain_player_id,
        vice_captain_player_id=solution.vice_captain_player_id,
        players=players_out,
    )


@router.get("", response_model=list[OptimizationRunSummaryOut])
def list_optimization_runs(
    recommendation_type: str | None = Query(None),
    db: Session = Depends(get_db),
) -> list[OptimizationRunSummaryOut]:
    """Most-recent-first history of squad/lineup runs (Milestone 8 item 1) -- both
    ``RecommendationType`` variants live in the same table, mirrors ``_load_run_response``'s
    own reuse across them."""
    stmt = select(OptimizationRun)
    if recommendation_type is not None:
        stmt = stmt.where(OptimizationRun.recommendation_type == recommendation_type)
    stmt = stmt.order_by(OptimizationRun.started_at.desc()).limit(_MAX_RESULTS)

    rows = db.execute(stmt).scalars().all()
    return [
        OptimizationRunSummaryOut(
            optimization_run_id=row.id,
            recommendation_type=row.recommendation_type.value,
            run_status=row.run_status.value,
            squad_total_cost=row.squad_total_cost,
            squad_projected_points=row.squad_projected_points,
            xi_projected_points=row.xi_projected_points,
            started_at=row.started_at,
            completed_at=row.completed_at,
        )
        for row in rows
    ]


def _load_transfer_run_response(
    db: Session, transfer_optimization_run_id: uuid.UUID
) -> TransferOptimizationRunOut:
    run = db.get(TransferOptimizationRun, transfer_optimization_run_id)
    assert run is not None  # a SUCCEEDED job always leaves its transfer_optimization_runs row behind

    scenario_rows = (
        db.execute(
            select(TransferOptimizationRunScenario)
            .where(
                TransferOptimizationRunScenario.transfer_optimization_run_id == transfer_optimization_run_id
            )
            .order_by(TransferOptimizationRunScenario.transfers_count)
        )
        .scalars()
        .all()
    )

    player_out = aliased(Player)
    player_in = aliased(Player)

    scenarios_out: list[TransferScenarioOut] = []
    for scenario in scenario_rows:
        move_rows = db.execute(
            select(TransferOptimizationRunMove, player_out.web_name, player_in.web_name)
            .join(player_out, TransferOptimizationRunMove.player_out_id == player_out.id)
            .join(player_in, TransferOptimizationRunMove.player_in_id == player_in.id)
            .where(TransferOptimizationRunMove.transfer_optimization_run_scenario_id == scenario.id)
            .order_by(TransferOptimizationRunMove.move_index)
        ).all()

        moves_out = [
            TransferMoveOut(
                player_out_id=move.player_out_id,
                player_out_web_name=out_web_name,
                player_in_id=move.player_in_id,
                player_in_web_name=in_web_name,
                player_out_selling_price=move.player_out_selling_price_snapshot,
                player_in_cost=move.player_in_cost_snapshot,
                projected_points_gain_over_horizon=move.projected_points_gain_over_horizon,
            )
            for move, out_web_name, in_web_name in move_rows
        ]

        scenarios_out.append(
            TransferScenarioOut(
                transfers_count=scenario.transfers_count,
                moves=moves_out,
                raw_horizon_points_gain=scenario.raw_horizon_points_gain,
                hit_cost=scenario.hit_cost,
                net_horizon_points_gain=scenario.net_horizon_points_gain,
                is_recommended=scenario.is_recommended,
            )
        )

    assert run.recommended_transfers_count is not None  # a SUCCEEDED run always sets this
    recommended_action = _RECOMMENDED_ACTION_BY_TRANSFERS_COUNT[run.recommended_transfers_count]

    return TransferOptimizationRunOut(
        transfer_optimization_run_id=run.id,
        manager_squad_snapshot_id=run.manager_squad_snapshot_id,
        run_status=run.run_status.value,
        horizon_gameweek_count=len(run.horizon_gameweek_ids_json or []),
        free_transfers_available=run.free_transfers_available_snapshot,
        bank=run.bank_snapshot,
        recommended_action=recommended_action,
        scenarios=scenarios_out,
        started_at=run.started_at,
        completed_at=run.completed_at,
    )


@router.post("/transfers", response_model=TransferOptimizationRunOut)
def optimize_transfers(
    body: TransferOptimizationRequest, db: Session = Depends(get_db)
) -> TransferOptimizationRunOut | JSONResponse:
    job = execute_job(
        db,
        job_type=JobType.TRANSFER_OPTIMIZATION,
        parameters={
            "manager_squad_snapshot_id": str(body.manager_squad_snapshot_id),
            "horizon_gameweeks": body.horizon_gameweeks,
        },
        requested_by=body.requested_by,
        trigger_type=TriggerType.API,
    )
    if job.status != JobStatus.SUCCEEDED:
        return _error_response(job)

    assert job.output_json is not None  # a SUCCEEDED job always has output_json
    transfer_optimization_run_id = uuid.UUID(job.output_json["transfer_optimization_run_id"])
    return _load_transfer_run_response(db, transfer_optimization_run_id)


@router.get("/transfers/{transfer_optimization_run_id}", response_model=TransferOptimizationRunOut)
def get_transfer_optimization_run(
    transfer_optimization_run_id: uuid.UUID, db: Session = Depends(get_db)
) -> TransferOptimizationRunOut | JSONResponse:
    """Fetch an already-completed transfer-optimization run by id (Milestone 8 Layer 1)."""
    if db.get(TransferOptimizationRun, transfer_optimization_run_id) is None:
        return _not_found_response("TRANSFER_OPTIMIZATION_RUN", transfer_optimization_run_id)
    return _load_transfer_run_response(db, transfer_optimization_run_id)


@router.get("/transfers", response_model=list[TransferOptimizationRunSummaryOut])
def list_transfer_optimization_runs(
    manager_squad_snapshot_id: uuid.UUID | None = Query(None),
    db: Session = Depends(get_db),
) -> list[TransferOptimizationRunSummaryOut]:
    """Most-recent-first history of transfer-optimization runs (Milestone 8 item 1)."""
    stmt = select(TransferOptimizationRun)
    if manager_squad_snapshot_id is not None:
        stmt = stmt.where(TransferOptimizationRun.manager_squad_snapshot_id == manager_squad_snapshot_id)
    stmt = stmt.order_by(TransferOptimizationRun.started_at.desc()).limit(_MAX_RESULTS)

    rows = db.execute(stmt).scalars().all()
    return [
        TransferOptimizationRunSummaryOut(
            transfer_optimization_run_id=row.id,
            manager_squad_snapshot_id=row.manager_squad_snapshot_id,
            run_status=row.run_status.value,
            recommended_action=(
                _RECOMMENDED_ACTION_BY_TRANSFERS_COUNT[row.recommended_transfers_count]
                if row.recommended_transfers_count is not None
                else None
            ),
            started_at=row.started_at,
            completed_at=row.completed_at,
        )
        for row in rows
    ]


# Registered AFTER every static single-segment GET above ("", "/transfers") -- Starlette
# matches routes in registration order, and this catch-all's pattern ("/{optimization_run_id}")
# is structurally identical (one path segment under /optimize) to "/transfers", so if it were
# registered first it would swallow "GET /optimize/transfers" too, trying (and failing) to
# parse "transfers" as a UUID.
@router.get("/{optimization_run_id}", response_model=OptimizationRunOut)
def get_optimization_run(
    optimization_run_id: uuid.UUID, db: Session = Depends(get_db)
) -> OptimizationRunOut | JSONResponse:
    """Fetch an already-completed initial-squad or lineup run by id (Milestone 8 Layer 1).

    Both ``recommendation_type`` variants live in the same ``optimization_runs``
    table, so one endpoint covers both -- mirrors ``_load_run_response``'s own
    reuse across ``/initial-squad`` and ``/lineup`` above.
    """
    if db.get(OptimizationRun, optimization_run_id) is None:
        return _not_found_response("OPTIMIZATION_RUN", optimization_run_id)
    return _load_run_response(db, optimization_run_id)


@router.post("/lineup", response_model=OptimizationRunOut)
def optimize_lineup(body: LineupRequest, db: Session = Depends(get_db)) -> OptimizationRunOut | JSONResponse:
    parameters: dict[str, str | list[str]]
    if body.source_optimization_run_id is not None:
        parameters = {"source_optimization_run_id": str(body.source_optimization_run_id)}
    else:
        # The LineupRequest validator guarantees exactly one of the two fields is set,
        # so player_ids is non-None here even though the static type is Optional.
        assert body.player_ids is not None
        parameters = {"player_ids": [str(pid) for pid in body.player_ids]}
    job = execute_job(
        db,
        job_type=JobType.LINEUP_OPTIMIZATION,
        parameters=parameters,
        requested_by=body.requested_by,
        trigger_type=TriggerType.API,
    )
    if job.status != JobStatus.SUCCEEDED:
        return _error_response(job)

    assert job.output_json is not None  # a SUCCEEDED job always has output_json
    optimization_run_id = uuid.UUID(job.output_json["optimization_run_id"])
    return _load_run_response(db, optimization_run_id)
