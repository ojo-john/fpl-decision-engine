"""POST /h2h/simulate endpoint.

Uses the request's own DB session for the outer ``Job`` bookkeeping via
``execute_job`` — the job handler internally opens its own session for
``h2h_simulation_runs``, exactly like ``optimize_transfers``/``sync_manager``
already do (see ``apps.api.routers.optimization``/``managers``); this is an
established pattern, not a new one.

Milestone 5 Layer 3 exposes H2H_SIMULATION only through this single
synchronous endpoint (mirrors ``/optimize/transfers``'s own shape) — a
run-listing/history endpoint is a natural follow-up, not built here.

Milestone 5 Layer 4 (risk-adjusted strategy comparison) extends this SAME
response with a nested ``strategy_candidates`` list rather than adding a new
``GET /h2h/simulations/{run_id}`` endpoint — every strategy candidate is
already fully computed by the time this endpoint's one synchronous
``H2H_SIMULATION`` job finishes, so there is no separate resource to fetch
later; a listing/history endpoint (across MULTIPLE runs) remains the natural
follow-up this module already called out, unchanged by this layer.
"""

import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.jobs.service import execute_job
from apps.api.dependencies import get_db
from apps.api.schemas.h2h import H2HSimulationRequest, H2HSimulationRunOut, H2HStrategyCandidateOut
from packages.database.models.h2h_simulation_run import H2HSimulationRun
from packages.database.models.h2h_strategy_candidate import H2HStrategyCandidate
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


def _load_strategy_candidates(db: Session, h2h_simulation_run_id: uuid.UUID) -> list[H2HStrategyCandidateOut]:
    rows = (
        db.execute(
            select(H2HStrategyCandidate)
            .where(H2HStrategyCandidate.h2h_simulation_run_id == h2h_simulation_run_id)
            .order_by(H2HStrategyCandidate.created_at)
        )
        .scalars()
        .all()
    )
    return [
        H2HStrategyCandidateOut(
            strategy_label=row.strategy_label.value,
            win_probability=row.win_probability,
            draw_probability=row.draw_probability,
            loss_probability=row.loss_probability,
            expected_margin=row.expected_margin,
            is_recommended=row.is_recommended,
            rationale=row.rationale,
            transfer_optimization_run_scenario_id=row.transfer_optimization_run_scenario_id,
            captain_player_id=row.captain_player_id,
        )
        for row in rows
    ]


def _load_run_response(db: Session, h2h_simulation_run_id: uuid.UUID) -> H2HSimulationRunOut:
    run = db.get(H2HSimulationRun, h2h_simulation_run_id)
    assert run is not None  # a SUCCEEDED job always leaves its h2h_simulation_runs row behind
    strategy_candidates = _load_strategy_candidates(db, h2h_simulation_run_id)
    recommended_strategy = next((c.strategy_label for c in strategy_candidates if c.is_recommended), None)
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
        recommended_strategy=recommended_strategy,
        h2h_adjustment_materially_changed=run.h2h_adjustment_materially_changed,
        strategy_candidates=strategy_candidates,
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
