"""GET /gameweeks/next-deadline -- the next unfinished gameweek's deadline, for a Dashboard
countdown. The real FPL squad-selection deadline is what actually matters to a manager (not
whatever this system's own optimization gate happens to permit), so this exists independent of
every gated optimization endpoint -- a manager needs to know the deadline is coming even while
the gate is still closed pre-season.
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.optimization.gate import check_optimization_gate
from apps.api.dependencies import get_db
from apps.api.schemas.gameweeks import NextDeadlineOut
from packages.database.models.gameweek import Gameweek
from packages.database.models.season import Season

router = APIRouter(prefix="/gameweeks", tags=["gameweeks"])


@router.get("/next-deadline", response_model=NextDeadlineOut)
def get_next_deadline(db: Session = Depends(get_db)) -> NextDeadlineOut | JSONResponse:
    """404s (not a stale/fabricated date) when there's no current season, or every gameweek for
    it is already finished (end of season)."""
    season_id = db.execute(select(Season.id).where(Season.is_current.is_(True))).scalar_one_or_none()
    if season_id is None:
        return JSONResponse(
            status_code=404, content={"error_code": "NO_CURRENT_SEASON", "detail": "No current season found."}
        )

    gameweek = db.execute(
        select(Gameweek)
        .where(Gameweek.season_id == season_id, Gameweek.finished.is_(False))
        .order_by(Gameweek.fpl_event_id)
        .limit(1)
    ).scalar_one_or_none()
    if gameweek is None:
        return JSONResponse(
            status_code=404,
            content={
                "error_code": "NO_UPCOMING_GAMEWEEK",
                "detail": "No unfinished gameweek exists (off-season).",
            },
        )

    gate = check_optimization_gate(db, season_id)
    return NextDeadlineOut(
        fpl_event_id=gameweek.fpl_event_id,
        name=gameweek.name,
        deadline_time=gameweek.deadline_time,
        gate_allowed=gate.allowed,
        gate_reason_code=gate.reason_code,
    )
