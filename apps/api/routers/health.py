"""Health check endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from apps.api.dependencies import get_db
from packages.database.health import check_database_health

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    """Pure liveness check — never touches the database, always 200."""
    return {"status": "ok"}


@router.get("/health/db")
def health_db(db: Session = Depends(get_db)) -> dict[str, str]:
    """Database connectivity check.

    Returns 200 if the database is reachable, 503 with a sanitized error
    detail otherwise. The raw connection string is never included in the
    response — see packages.database.health.check_database_health.
    """
    bind = db.get_bind()
    assert isinstance(bind, Engine), "Session is not bound to an Engine"
    result = check_database_health(bind)
    if not result.healthy:
        raise HTTPException(status_code=503, detail=result.detail)
    return {"status": "ok", "detail": result.detail}
