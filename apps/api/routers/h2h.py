"""POST /h2h/simulate endpoint.

Uses the request's own DB session for the outer ``Job`` bookkeeping via
``execute_job`` — the job handler internally opens its own session for
``h2h_simulation_runs``, exactly like ``optimize_transfers``/``sync_manager``
already do (see ``apps.api.routers.optimization``/``managers``); this is an
established pattern, not a new one.

Milestone 5 Layer 3 exposes H2H_SIMULATION only through this single
synchronous endpoint (mirrors ``/optimize/transfers``'s own shape) — a
run-listing/history endpoint is a natural follow-up, not built here.
"""

import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.jobs.service import execute_job
from apps.api.dependencies import get_db
from apps.api.schemas.h2h import H2HSimulationRequest, H2HSimulationRunOut
from packages.database.models.h2h_simulation_run import H2HSimulationRun
from packages.domain.jobs import JobStatus, JobType, TriggerType

router = APIRouter(prefix="/h2h", tags=["h2h"])


def _error_response(job) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "error_code": job.error_code,
            "error_message": job.error_message,
            "job_id": str(job.id),
        },
    )


def _load_run_response(db: Session, h2h_simulation_run_id: uuid.UUID) -> H2HSimulationRunOut:
    run = db.get(H2HSimulationRun, h2h_simulation_run_id)
    assert run is not None  # a SUCCEEDED job always leaves its h2h_simulation_runs row behind
    return H2HSimulationRunOut(
        h2h_simulation_run_id=run.id,
        run_status=run.run_status.value,
        target_fpl_event_id=run.target_fpl_event_id,
        simulation_method_version=run.simulation_method_version,
        sample_count=run.sample_count,
        user_score_mean=run.user_score_mean,
        user_score_floor=run.user_score_floor,
        user_score_ceiling=run.user_score_ceiling,
        rival_score_mean=run.rival_score_mean,
        rival_score_floor=run.rival_score_floor,
        rival_score_ceiling=run.rival_score_ceiling,
        win_probability=run.win_probability,
        draw_probability=run.draw_probability,
        loss_probability=run.loss_probability,
        expected_margin=run.expected_margin,
        margin_std_dev=run.margin_std_dev,
        confidence=run.confidence.value if run.confidence is not None else None,
        started_at=run.started_at,
        completed_at=run.completed_at,
    )


@router.post("/simulate", response_model=H2HSimulationRunOut)
def simulate_h2h_matchup(
    body: H2HSimulationRequest, db: Session = Depends(get_db)
) -> H2HSimulationRunOut | JSONResponse:
    parameters: dict[str, str | int] = {
        "manager_squad_snapshot_id": str(body.manager_squad_snapshot_id),
        "rival_profile_id": str(body.rival_profile_id),
    }
    if body.h2h_match_fpl_id is not None:
        parameters["h2h_match_fpl_id"] = body.h2h_match_fpl_id
    if body.target_fpl_event_id is not None:
        parameters["target_fpl_event_id"] = body.target_fpl_event_id

    job = execute_job(
        db,
        job_type=JobType.H2H_SIMULATION,
        parameters=parameters,
        requested_by=body.requested_by,
        trigger_type=TriggerType.API,
    )
    if job.status != JobStatus.SUCCEEDED:
        return _error_response(job)

    assert job.output_json is not None  # a SUCCEEDED job always has output_json
    h2h_simulation_run_id = uuid.UUID(job.output_json["h2h_simulation_run_id"])
    return _load_run_response(db, h2h_simulation_run_id)
