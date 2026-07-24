"""POST /h2h/simulate endpoint, plus GET-by-id and GET list (history).

Uses the request's own DB session for the outer ``Job`` bookkeeping via
``execute_job`` — the job handler internally opens its own session for
``h2h_simulation_runs``, exactly like ``optimize_transfers``/``sync_manager``
already do (see ``apps.api.routers.optimization``/``managers``); this is an
established pattern, not a new one.

Milestone 5 Layer 4 (risk-adjusted strategy comparison) extends the by-id
response with a nested ``strategy_candidates`` list rather than adding a new
``GET /h2h/simulations/{run_id}`` endpoint — every strategy candidate is
already fully computed by the time this endpoint's one synchronous
``H2H_SIMULATION`` job finishes, so there is no separate resource to fetch
later.
"""

import uuid

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.jobs.service import execute_job
from apps.api.dependencies import get_db
from apps.api.schemas.h2h import (
    H2HSimulationRequest,
    H2HSimulationRunOut,
    H2HSimulationRunSummaryOut,
    H2HStrategyCandidateOut,
)
from packages.database.models.h2h_simulation_run import H2HSimulationRun
from packages.database.models.h2h_strategy_candidate import H2HStrategyCandidate
from packages.domain.jobs import JobStatus, JobType, TriggerType

# Same "capped read, not full pagination" convention as apps.api.routers.optimization/managers.
_MAX_RESULTS = 200

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


def _not_found_response(h2h_simulation_run_id: uuid.UUID) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={
            "error_code": "H2H_SIMULATION_RUN_NOT_FOUND",
            "detail": f"h2h_simulation_runs row {h2h_simulation_run_id} not found.",
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


@router.get("/simulate", response_model=list[H2HSimulationRunSummaryOut])
def list_h2h_simulation_runs(
    manager_squad_snapshot_id: uuid.UUID | None = Query(None),
    rival_profile_id: uuid.UUID | None = Query(None),
    db: Session = Depends(get_db),
) -> list[H2HSimulationRunSummaryOut]:
    """Most-recent-first history of H2H simulation runs (Milestone 8 item 1)."""
    stmt = select(H2HSimulationRun)
    if manager_squad_snapshot_id is not None:
        stmt = stmt.where(H2HSimulationRun.manager_squad_snapshot_id == manager_squad_snapshot_id)
    if rival_profile_id is not None:
        stmt = stmt.where(H2HSimulationRun.rival_profile_id == rival_profile_id)
    stmt = stmt.order_by(H2HSimulationRun.started_at.desc()).limit(_MAX_RESULTS)

    rows = db.execute(stmt).scalars().all()
    return [
        H2HSimulationRunSummaryOut(
            h2h_simulation_run_id=row.id,
            manager_squad_snapshot_id=row.manager_squad_snapshot_id,
            rival_profile_id=row.rival_profile_id,
            run_status=row.run_status.value,
            target_fpl_event_id=row.target_fpl_event_id,
            win_probability=row.win_probability,
            draw_probability=row.draw_probability,
            loss_probability=row.loss_probability,
            started_at=row.started_at,
            completed_at=row.completed_at,
        )
        for row in rows
    ]


@router.get("/simulate/{h2h_simulation_run_id}", response_model=H2HSimulationRunOut)
def get_h2h_simulation_run(
    h2h_simulation_run_id: uuid.UUID, db: Session = Depends(get_db)
) -> H2HSimulationRunOut | JSONResponse:
    """Fetch an already-completed H2H simulation run by id (Milestone 8 Layer 1)."""
    if db.get(H2HSimulationRun, h2h_simulation_run_id) is None:
        return _not_found_response(h2h_simulation_run_id)
    return _load_run_response(db, h2h_simulation_run_id)
