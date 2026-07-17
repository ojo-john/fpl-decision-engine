"""``/recommendations`` endpoints: trigger and read ``recommendation_audits`` rows.

Milestone 7 Layer 1. ``POST /recommendations/generate`` mirrors ``/optimize/chips``'s
exact shape (``execute_job`` -> 409-with-``error_code`` on non-success -> assembled
response on success). ``GET /recommendations/latest``/``GET /recommendations/{id}``
assemble a compact response with per-component SUMMARIES (not a full nested
re-embedding of everything each component's own dedicated endpoint already
exposes -- see ``apps.api.schemas.recommendations.RecommendationComponentSummaryOut``'s
own docstring) -- a caller who needs full detail follows the returned ``run_id``
to that component's own endpoint (``/optimize/chips``, ``/h2h/simulate``, etc.).
"""

import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.jobs.service import execute_job
from apps.api.dependencies import get_db
from apps.api.schemas.recommendations import (
    RecommendationAuditOut,
    RecommendationComponentSummaryOut,
    RecommendationGenerationRequest,
)
from packages.database.models.chip_optimization_run import ChipOptimizationRun
from packages.database.models.h2h_simulation_run import H2HSimulationRun
from packages.database.models.optimization_run import OptimizationRun
from packages.database.models.recommendation_audit import RecommendationAudit
from packages.database.models.season import Season
from packages.database.models.transfer_optimization_run import TransferOptimizationRun
from packages.domain.jobs import JobStatus, JobType, TriggerType

router = APIRouter(prefix="/recommendations", tags=["recommendations"])


def _error_response(job) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "error_code": job.error_code,
            "error_message": job.error_message,
            "job_id": str(job.id),
        },
    )


def _not_found_response(recommendation_audit_id: uuid.UUID | None = None) -> JSONResponse:
    detail = (
        f"recommendation_audits row {recommendation_audit_id} not found."
        if recommendation_audit_id is not None
        else "No authoritative recommendation_audits row exists for the current season yet."
    )
    return JSONResponse(
        status_code=404, content={"error_code": "RECOMMENDATION_AUDIT_NOT_FOUND", "detail": detail}
    )


def _optimization_summary(db: Session, run_id: uuid.UUID | None) -> RecommendationComponentSummaryOut | None:
    if run_id is None:
        return None
    run = db.get(OptimizationRun, run_id)
    if run is None:
        return None
    return RecommendationComponentSummaryOut(
        run_id=run.id,
        summary=f"{run.recommendation_type.value}: xi_projected_points={run.xi_projected_points}",
    )


def _transfer_optimization_summary(
    db: Session, run_id: uuid.UUID | None
) -> RecommendationComponentSummaryOut | None:
    if run_id is None:
        return None
    run = db.get(TransferOptimizationRun, run_id)
    if run is None:
        return None
    return RecommendationComponentSummaryOut(
        run_id=run.id,
        summary=(
            f"{run.recommended_transfers_count} transfer(s) recommended, "
            f"net gain {run.recommended_net_horizon_points_gain}"
        ),
    )


def _h2h_simulation_summary(
    db: Session, run_id: uuid.UUID | None
) -> RecommendationComponentSummaryOut | None:
    if run_id is None:
        return None
    run = db.get(H2HSimulationRun, run_id)
    if run is None:
        return None
    return RecommendationComponentSummaryOut(run_id=run.id, summary=f"win_probability={run.win_probability}")


def _chip_optimization_summary(
    db: Session, run_id: uuid.UUID | None
) -> RecommendationComponentSummaryOut | None:
    if run_id is None:
        return None
    run = db.get(ChipOptimizationRun, run_id)
    if run is None:
        return None
    return RecommendationComponentSummaryOut(
        run_id=run.id, summary=f"Chip evaluation completed for GW{run.target_fpl_event_id}"
    )


def _assembled_response(db: Session, audit: RecommendationAudit) -> RecommendationAuditOut:
    return RecommendationAuditOut(
        recommendation_audit_id=audit.id,
        season_id=audit.season_id,
        target_gameweek_id=audit.target_gameweek_id,
        target_fpl_event_id=audit.target_fpl_event_id,
        is_authoritative=audit.is_authoritative,
        confidence=audit.confidence.value,
        confidence_drivers=list(audit.confidence_drivers_json or []),
        assumptions=list(audit.assumptions_json or []),
        known_risks=list(audit.known_risks_json or []),
        champion_model_version_id=audit.champion_model_version_id,
        fpl_api_health_at_generation=(
            audit.fpl_api_health_at_generation.value
            if audit.fpl_api_health_at_generation is not None
            else None
        ),
        optimization=_optimization_summary(db, audit.optimization_run_id),
        transfer_optimization=_transfer_optimization_summary(db, audit.transfer_optimization_run_id),
        h2h_simulation=_h2h_simulation_summary(db, audit.h2h_simulation_run_id),
        chip_optimization=_chip_optimization_summary(db, audit.chip_optimization_run_id),
        generated_at=audit.generated_at,
    )


@router.post("/generate", response_model=RecommendationAuditOut)
def generate_recommendation(
    body: RecommendationGenerationRequest, db: Session = Depends(get_db)
) -> RecommendationAuditOut | JSONResponse:
    parameters: dict[str, str | int] = {}
    if body.manager_squad_snapshot_id is not None:
        parameters["manager_squad_snapshot_id"] = str(body.manager_squad_snapshot_id)
    if body.target_fpl_event_id is not None:
        parameters["target_fpl_event_id"] = body.target_fpl_event_id

    job = execute_job(
        db,
        job_type=JobType.RECOMMENDATION_GENERATION,
        parameters=parameters,
        requested_by=body.requested_by,
        trigger_type=TriggerType.API,
    )
    if job.status != JobStatus.SUCCEEDED:
        return _error_response(job)

    assert job.output_json is not None  # a SUCCEEDED job always has output_json
    recommendation_audit_id = uuid.UUID(job.output_json["recommendation_audit_id"])
    audit = db.get(RecommendationAudit, recommendation_audit_id)
    assert audit is not None  # a SUCCEEDED job always leaves its recommendation_audits row behind
    return _assembled_response(db, audit)


@router.get("/latest", response_model=RecommendationAuditOut)
def get_latest_recommendation(db: Session = Depends(get_db)) -> RecommendationAuditOut | JSONResponse:
    audit = db.execute(
        select(RecommendationAudit)
        .join(Season, Season.id == RecommendationAudit.season_id)
        .where(Season.is_current.is_(True), RecommendationAudit.is_authoritative.is_(True))
        .order_by(RecommendationAudit.generated_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if audit is None:
        return _not_found_response()
    return _assembled_response(db, audit)


@router.get("/{recommendation_audit_id}", response_model=RecommendationAuditOut)
def get_recommendation(
    recommendation_audit_id: uuid.UUID, db: Session = Depends(get_db)
) -> RecommendationAuditOut | JSONResponse:
    audit = db.get(RecommendationAudit, recommendation_audit_id)
    if audit is None:
        return _not_found_response(recommendation_audit_id)
    return _assembled_response(db, audit)
