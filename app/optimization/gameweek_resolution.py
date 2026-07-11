"""Resolves the target gameweek an optimization run should project against.

Handles the recurring FPL close-season gap gracefully: when every gameweek
in a season is finished (the normal state every summer), there is no active
gameweek to target — this is not an error condition.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.database.models.gameweek import Gameweek
from packages.domain.optimization import TargetGameweekStatus


def resolve_target_gameweek(
    session: Session, season_id: uuid.UUID
) -> tuple[uuid.UUID | None, TargetGameweekStatus]:
    """Return the earliest unfinished gameweek (by deadline) for a season, if any.

    Returns ``(None, NO_ACTIVE_GAMEWEEK)`` if every gameweek in the season is
    finished.
    """
    gameweek_id = session.execute(
        select(Gameweek.id)
        .where(Gameweek.season_id == season_id, Gameweek.finished.is_(False))
        .order_by(Gameweek.deadline_time.asc())
        .limit(1)
    ).scalar_one_or_none()

    if gameweek_id is None:
        return None, TargetGameweekStatus.NO_ACTIVE_GAMEWEEK
    return gameweek_id, TargetGameweekStatus.RESOLVED
