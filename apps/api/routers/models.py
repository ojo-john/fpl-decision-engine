"""``GET /models/champion`` (Milestone 8): the current champion model_versions row, if any.

The only way to discover a ``model_version_id`` from apps/web before this was
``RecommendationAuditOut.champion_model_version_id`` -- which requires a
recommendation to already exist. This is the direct, always-available
lookup, primarily so ``POST /jobs/backtest`` (which requires a
``model_version_id`` with no sensible default -- see
``app.jobs.handlers.backtest``'s own docstring) has something to point at
from the UI without a full model-version browser.
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.dependencies import get_db
from apps.api.schemas.models import ChampionModelOut
from packages.database.models.model_version import ModelVersion

router = APIRouter(prefix="/models", tags=["models"])


@router.get("/champion", response_model=ChampionModelOut)
def get_champion_model(db: Session = Depends(get_db)) -> ChampionModelOut | JSONResponse:
    version = db.execute(
        select(ModelVersion).where(ModelVersion.is_champion.is_(True))
    ).scalar_one_or_none()
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
