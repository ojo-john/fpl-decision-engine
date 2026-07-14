"""Resolves the target gameweek(s) an optimization run should project against.

Handles the recurring FPL close-season gap gracefully: when every gameweek
in a season is finished (the normal state every summer), there is no active
gameweek to target — this is not an error condition.

``resolve_target_gameweek`` (single gameweek, Milestone 3) and
``resolve_horizon_gameweeks`` (multi-gameweek, Milestone 4 Layer 2) share the
exact same "what counts as unfinished, in what order" query via
``_unfinished_gameweek_ids_ordered`` below, rather than duplicating it —
both are just different-sized slices of the same ascending-deadline,
unfinished-only ordering.

``resolve_most_recent_finished_gameweek_event`` (Milestone 5 Layer 1) is the
deliberate mirror image of that query: LEAGUE_SYNC needs the most recent
*finished* gameweek (to fetch rivals' already-played picks), not the next
*unfinished* one — the two queries share nothing but the same table, so it
is not built by reusing ``_unfinished_gameweek_ids_ordered`` with inverted
filters.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.database.models.gameweek import Gameweek
from packages.domain.optimization import TargetGameweekStatus


def _unfinished_gameweek_ids_ordered(
    session: Session, season_id: uuid.UUID, limit: int | None = None
) -> list[uuid.UUID]:
    """Unfinished gameweek ids for a season, ascending by ``deadline_time``, optionally capped."""
    stmt = (
        select(Gameweek.id)
        .where(Gameweek.season_id == season_id, Gameweek.finished.is_(False))
        .order_by(Gameweek.deadline_time.asc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.execute(stmt).scalars().all())


def resolve_target_gameweek(
    session: Session, season_id: uuid.UUID
) -> tuple[uuid.UUID | None, TargetGameweekStatus]:
    """Return the earliest unfinished gameweek (by deadline) for a season, if any.

    Returns ``(None, NO_ACTIVE_GAMEWEEK)`` if every gameweek in the season is
    finished.
    """
    gameweek_ids = _unfinished_gameweek_ids_ordered(session, season_id, limit=1)

    if not gameweek_ids:
        return None, TargetGameweekStatus.NO_ACTIVE_GAMEWEEK
    return gameweek_ids[0], TargetGameweekStatus.RESOLVED


def resolve_horizon_gameweeks(session: Session, season_id: uuid.UUID, horizon_length: int) -> list[uuid.UUID]:
    """The earliest unfinished gameweek plus up to ``horizon_length - 1`` more, by ascending deadline.

    Gracefully truncates near season end (returns fewer than
    ``horizon_length`` gameweek ids, never errors) — same philosophy as
    ``resolve_target_gameweek``'s ``NO_ACTIVE_GAMEWEEK`` handling. Returns an
    empty list if there are zero unfinished gameweeks at all (the real
    off-season state this codebase has hit repeatedly in live testing) —
    callers must treat an empty list as "nothing to plan for", not an error.

    ``horizon_length`` must be a positive integer: this is a caller
    configuration value (e.g. a hand-maintained constant), not user/external
    input, and a non-positive value here is always a misconfiguration, never
    a legitimate request. Raises ``ValueError`` rather than silently
    returning ``[]`` — an empty return here would otherwise be
    indistinguishable from the genuine, normal off-season state this
    function is explicitly designed to signal gracefully, masking a real bug
    as a benign no-op.
    """
    if horizon_length < 1:
        raise ValueError(f"horizon_length must be a positive integer, got {horizon_length}")
    return _unfinished_gameweek_ids_ordered(session, season_id, limit=horizon_length)


def resolve_most_recent_finished_gameweek_event(session: Session, season_id: uuid.UUID) -> int | None:
    """Return the raw FPL ``event`` number of the most recently finished gameweek, if any.

    Ordered by ``deadline_time`` descending among ``finished=True`` rows only.
    Returns the raw ``fpl_event_id`` (not the internal UUID) because callers
    (``LEAGUE_SYNC``) hand this straight to
    ``FPLClient.fetch_manager_picks(manager_id, event)``, which speaks FPL's
    raw event numbering.

    Returns ``None`` if no gameweek has finished yet this season (e.g. the
    season hasn't kicked off) — callers must treat this as "no rival picks
    history exists yet", not an error.
    """
    return session.execute(
        select(Gameweek.fpl_event_id)
        .where(Gameweek.season_id == season_id, Gameweek.finished.is_(True))
        .order_by(Gameweek.deadline_time.desc())
        .limit(1)
    ).scalar_one_or_none()
