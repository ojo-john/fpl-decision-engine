"""POST /optimize/* endpoints.

Uses the request's own DB session for the outer ``Job`` bookkeeping via
``execute_job`` — the job handler internally opens its own session for
``optimization_runs``, exactly like ``DataIngestionJobRunner`` already does
(see ``app/jobs/handlers/data_ingestion.py``); this is an established
pattern, not a new one.
"""

import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from app.jobs.service import execute_job
from apps.api.dependencies import get_db
from apps.api.schemas.optimization import (
    InitialSquadRequest,
    LineupRequest,
    OptimizationRunOut,
    OptimizationRunPlayerOut,
    TransferMoveOut,
    TransferOptimizationRequest,
    TransferOptimizationRunOut,
    TransferScenarioOut,
)
from packages.database.models.optimization_run import OptimizationRun
from packages.database.models.optimization_run_player import OptimizationRunPlayer
from packages.database.models.player import Player
from packages.database.models.position import Position
from packages.database.models.transfer_optimization_run import TransferOptimizationRun
from packages.database.models.transfer_optimization_run_move import TransferOptimizationRunMove
from packages.database.models.transfer_optimization_run_scenario import TransferOptimizationRunScenario
from packages.domain.jobs import JobStatus, JobType, TriggerType

_RECOMMENDED_ACTION_BY_TRANSFERS_COUNT = {0: "roll", 1: "one_transfer", 2: "two_transfers"}

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
