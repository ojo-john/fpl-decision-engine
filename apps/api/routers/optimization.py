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
from sqlalchemy.orm import Session

from app.jobs.service import execute_job
from apps.api.dependencies import get_db
from apps.api.schemas.optimization import (
    InitialSquadRequest,
    LineupRequest,
    OptimizationRunOut,
    OptimizationRunPlayerOut,
)
from packages.database.models.optimization_run import OptimizationRun
from packages.database.models.optimization_run_player import OptimizationRunPlayer
from packages.database.models.player import Player
from packages.database.models.position import Position
from packages.domain.jobs import JobStatus, JobType, TriggerType

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
