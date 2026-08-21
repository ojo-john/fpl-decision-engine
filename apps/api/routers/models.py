"""``GET /models/champion`` (Milestone 8): the current champion model_versions row, if any.

The only way to discover a ``model_version_id`` from apps/web before this was
``RecommendationAuditOut.champion_model_version_id`` -- which requires a
recommendation to already exist. This is the direct, always-available
lookup, primarily so ``POST /jobs/backtest`` (which requires a
``model_version_id`` with no sensible default -- see
``app.jobs.handlers.backtest``'s own docstring) has something to point at
from the UI without a full model-version browser.

Also adds the ``/models/start-probability`` trio (train / list / get-by-id) for the SEPARATE,
additive auxiliary start-probability classifier -- see
``app.jobs.handlers.start_probability_training``'s own module docstring for what this model is and
why it is deliberately NOT wired into ``/models/champion`` or any promotion flow. There is
deliberately no ``POST .../activate`` endpoint here yet -- see
``packages.database.models.start_probability_model``'s own docstring: activating a trained row is
left a manual, reviewed decision (direct DB update) rather than a one-click API action, since this
mechanism was built specifically so a human reviews real backtest evidence before it can affect
any live projection.
"""

import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.jobs.service import execute_job
from apps.api.dependencies import get_db
from apps.api.schemas.models import (
    ChampionModelOut,
    StartProbabilityModelOut,
    StartProbabilityTrainingRequest,
)
from packages.database.models.model_version import ModelVersion
from packages.database.models.start_probability_model import StartProbabilityModel
from packages.domain.jobs import JobStatus, JobType, TriggerType

router = APIRouter(prefix="/models", tags=["models"])

_MAX_RESULTS = 50


@router.get("/champion", response_model=ChampionModelOut)
def get_champion_model(db: Session = Depends(get_db)) -> ChampionModelOut | JSONResponse:
    version = db.execute(select(ModelVersion).where(ModelVersion.is_champion.is_(True))).scalar_one_or_none()
    if version is None:
        return JSONResponse(
            status_code=404,
            content={
                "error_code": "NO_CHAMPION_MODEL",
                "detail": "No champion model has been selected yet.",
            },
        )
    return ChampionModelOut(
        model_version_id=version.id,
        model_name=version.model_name,
        model_family=version.model_family.value,
        feature_version=version.feature_version,
        trained_at=version.trained_at,
    )


def _assembled_start_probability_model(row: StartProbabilityModel) -> StartProbabilityModelOut:
    return StartProbabilityModelOut(
        start_probability_model_id=row.id,
        feature_version=row.feature_version,
        target_variable=row.target_variable,
        usable_seasons=row.usable_seasons_json,
        fold_count=len(row.fold_metrics_json),
        degenerate_single_season=row.degenerate_single_season,
        aggregate_auc=row.aggregate_auc,
        aggregate_accuracy=row.aggregate_accuracy,
        aggregate_f1=row.aggregate_f1,
        aggregate_log_loss=row.aggregate_log_loss,
        n_observations=row.n_observations,
        is_active=row.is_active,
        trained_at=row.trained_at,
    )


@router.post("/start-probability-training", response_model=StartProbabilityModelOut)
def run_start_probability_training(
    body: StartProbabilityTrainingRequest, db: Session = Depends(get_db)
) -> StartProbabilityModelOut | JSONResponse:
    job = execute_job(
        db,
        job_type=JobType.START_PROBABILITY_MODEL_TRAINING,
        parameters={"feature_version": body.feature_version} if body.feature_version is not None else None,
        requested_by=body.requested_by,
        trigger_type=TriggerType.API,
    )
    if job.status != JobStatus.SUCCEEDED:
        return JSONResponse(
            status_code=409,
            content={"error_code": job.error_code, "error_message": job.error_message, "job_id": str(job.id)},
        )

    assert job.output_json is not None  # a SUCCEEDED job always has output_json
    row_id = uuid.UUID(job.output_json["start_probability_model_id"])
    row = db.get(StartProbabilityModel, row_id)
    assert row is not None  # a SUCCEEDED job always leaves its start_probability_models row behind
    return _assembled_start_probability_model(row)


@router.get("/start-probability", response_model=list[StartProbabilityModelOut])
def list_start_probability_models(db: Session = Depends(get_db)) -> list[StartProbabilityModelOut]:
    rows = (
        db.execute(
            select(StartProbabilityModel)
            .order_by(StartProbabilityModel.trained_at.desc())
            .limit(_MAX_RESULTS)
        )
        .scalars()
        .all()
    )
    return [_assembled_start_probability_model(row) for row in rows]


@router.get("/start-probability/{start_probability_model_id}", response_model=StartProbabilityModelOut)
def get_start_probability_model(
    start_probability_model_id: uuid.UUID, db: Session = Depends(get_db)
) -> StartProbabilityModelOut | JSONResponse:
    row = db.get(StartProbabilityModel, start_probability_model_id)
    if row is None:
        return JSONResponse(
            status_code=404,
            content={
                "error_code": "START_PROBABILITY_MODEL_NOT_FOUND",
                "detail": f"start_probability_models row {start_probability_model_id} not found.",
            },
        )
    return _assembled_start_probability_model(row)
