"""POST /optimize/chips endpoint.

Uses the request's own DB session for the outer ``Job`` bookkeeping via
``execute_job`` — the job handler internally opens its own session for
``chip_optimization_runs``, exactly like ``apps.api.routers.h2h``/
``apps.api.routers.optimization`` already do; an established pattern, not a
new one.

Milestone 6 Layer 3 exposes CHIP_SIMULATION only through this single
synchronous endpoint (mirrors ``/h2h/simulate``'s own shape) — a
run-listing/history endpoint is a natural follow-up, not built here.
"""

import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.jobs.service import execute_job
from apps.api.dependencies import get_db
from apps.api.schemas.chips import (
    ChipOptimizationRunOut,
    ChipRecommendationOut,
    ChipSimulationRequest,
    ChipWindowCandidateOut,
)
from packages.database.models.chip_optimization_run import ChipOptimizationRun
from packages.database.models.chip_recommendation import ChipRecommendation
from packages.database.models.chip_window_candidate import ChipWindowCandidate
from packages.domain.jobs import JobStatus, JobType, TriggerType

router = APIRouter(prefix="/optimize", tags=["chips"])


def _error_response(job) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "error_code": job.error_code,
            "error_message": job.error_message,
            "job_id": str(job.id),
        },
    )


def _not_found_response(chip_optimization_run_id: uuid.UUID) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={
            "error_code": "CHIP_OPTIMIZATION_RUN_NOT_FOUND",
            "detail": f"chip_optimization_runs row {chip_optimization_run_id} not found.",
        },
    )


def _candidate_out(row: ChipWindowCandidate) -> ChipWindowCandidateOut:
    return ChipWindowCandidateOut(
        candidate_fpl_event_id=row.candidate_fpl_event_id,
        gain_mean=row.gain_mean,
        gain_floor=row.gain_floor,
        gain_ceiling=row.gain_ceiling,
        is_blank_gameweek=row.is_blank_gameweek,
        is_double_gameweek=row.is_double_gameweek,
        rank_within_chip_type=row.rank_within_chip_type,
        h2h_win_probability_with_chip=row.h2h_win_probability_with_chip,
        h2h_win_probability_without_chip=row.h2h_win_probability_without_chip,
    )


def _load_recommendations(db: Session, chip_optimization_run_id: uuid.UUID) -> list[ChipRecommendationOut]:
    recommendation_rows = (
        db.execute(
            select(ChipRecommendation)
            .where(ChipRecommendation.chip_optimization_run_id == chip_optimization_run_id)
            .order_by(ChipRecommendation.created_at)
        )
        .scalars()
        .all()
    )
    candidate_rows = (
        db.execute(
            select(ChipWindowCandidate)
            .where(ChipWindowCandidate.chip_optimization_run_id == chip_optimization_run_id)
            .order_by(ChipWindowCandidate.rank_within_chip_type)
        )
        .scalars()
        .all()
    )
    candidates_by_id = {row.id: row for row in candidate_rows}
    candidates_by_chip_type: dict = {}
    for row in candidate_rows:
        candidates_by_chip_type.setdefault(row.chip_type, []).append(row)

    recommendations: list[ChipRecommendationOut] = []
    for recommendation_row in recommendation_rows:
        recommended_candidate_row = (
            candidates_by_id.get(recommendation_row.recommended_candidate_id)
            if recommendation_row.recommended_candidate_id is not None
            else None
        )
        recommendations.append(
            ChipRecommendationOut(
                chip_type=recommendation_row.chip_type.value,
                verdict=recommendation_row.verdict.value,
                rationale=recommendation_row.rationale,
                recommended_candidate=(
                    _candidate_out(recommended_candidate_row)
                    if recommended_candidate_row is not None
                    else None
                ),
                candidates=[
                    _candidate_out(row)
                    for row in candidates_by_chip_type.get(recommendation_row.chip_type, [])
                ],
            )
        )
    return recommendations


def _load_run_response(db: Session, chip_optimization_run_id: uuid.UUID) -> ChipOptimizationRunOut:
    run = db.get(ChipOptimizationRun, chip_optimization_run_id)
    assert run is not None  # a SUCCEEDED job always leaves its chip_optimization_runs row behind
    return ChipOptimizationRunOut(
        chip_optimization_run_id=run.id,
        run_status=run.run_status.value,
        target_fpl_event_id=run.target_fpl_event_id,
        recommendations=_load_recommendations(db, chip_optimization_run_id),
        started_at=run.started_at,
        completed_at=run.completed_at,
    )


@router.post("/chips", response_model=ChipOptimizationRunOut)
def simulate_chip_optimization(
    body: ChipSimulationRequest, db: Session = Depends(get_db)
) -> ChipOptimizationRunOut | JSONResponse:
    parameters: dict[str, str | list[str]] = {
        "manager_squad_snapshot_id": str(body.manager_squad_snapshot_id),
    }
    if body.chip_types is not None:
        parameters["chip_types"] = body.chip_types

    job = execute_job(
        db,
        job_type=JobType.CHIP_SIMULATION,
        parameters=parameters,
        requested_by=body.requested_by,
        trigger_type=TriggerType.API,
    )
    if job.status != JobStatus.SUCCEEDED:
        return _error_response(job)

    assert job.output_json is not None  # a SUCCEEDED job always has output_json
    chip_optimization_run_id = uuid.UUID(job.output_json["chip_optimization_run_id"])
    return _load_run_response(db, chip_optimization_run_id)


@router.get("/chips/{chip_optimization_run_id}", response_model=ChipOptimizationRunOut)
def get_chip_optimization_run(
    chip_optimization_run_id: uuid.UUID, db: Session = Depends(get_db)
) -> ChipOptimizationRunOut | JSONResponse:
    """Fetch an already-completed chip-optimization run by id (Milestone 8 Layer 1)."""
    if db.get(ChipOptimizationRun, chip_optimization_run_id) is None:
        return _not_found_response(chip_optimization_run_id)
    return _load_run_response(db, chip_optimization_run_id)
