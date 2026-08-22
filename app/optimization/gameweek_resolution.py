"""Resolves the target gameweek(s) an optimization run should project against.

Handles the recurring FPL close-season gap gracefully: when every gameweek
in a season has a passed deadline (the normal state every summer), there is
no active gameweek to target — this is not an error condition.

``resolve_target_gameweek`` (single gameweek, Milestone 3) and
``resolve_horizon_gameweeks`` (multi-gameweek, Milestone 4 Layer 2) share the
exact same "what counts as still actionable, in what order" query via
``_actionable_gameweek_ids_ordered`` below, rather than duplicating it —
both are just different-sized slices of the same ascending-deadline,
actionable-only ordering.

"Actionable" means ``deadline_time`` has not yet passed — NOT the same as
"unfinished" (``finished=False``), which only flips to ``True`` once every
match in that gameweek has actually been *played*, days after its deadline.
A gameweek whose deadline has passed but hasn't finished playing yet is
still ``finished=False`` but is no longer actionable: transfers/lineups for
it are already locked, so recommending against it is useless to a manager.
Confirmed as a real, live bug (2026-08-22): the old ``finished``-based
filter kept resolving a gameweek as the "target" for hours after its
deadline passed, for every one of the functions below except
``resolve_most_recent_finished_gameweek_event`` (see its own docstring for
why ``finished`` genuinely is the right filter there, not a bug).

``resolve_most_recent_finished_gameweek_event`` (Milestone 5 Layer 1) is the
deliberate mirror image of that query: LEAGUE_SYNC needs the most recent
*finished* gameweek (to fetch rivals' already-played picks), not the next
*actionable* one — the two queries share nothing but the same table, so it
is not built by reusing ``_actionable_gameweek_ids_ordered`` with inverted
filters.

``resolve_gameweeks_within_window`` (Milestone 6 Layer 2: Chip Scenario
Ranking) is a trivial extra filter on the SAME actionable-gameweek ordering
query — a chip's scheduling window (``app.chips.windows.ChipWindow``) bounds
how far into the future that specific chip can still be evaluated, so Chip
Scenario Ranking needs "actionable AND still within this window" rather than
either existing query's own unbounded/fixed-count slice.

Every public function below takes an explicit ``now: datetime`` parameter
(no default) rather than calling ``datetime.now(UTC)`` internally — this
codebase has no Clock/time-provider abstraction, but this mirrors the one
existing precedent for testable time-based logic
(``app.scheduling.tick.is_interval_due``). Callers that already compute a
``started_at = datetime.now(UTC)`` timestamp near the top of their own
handler should pass that same value through rather than taking a second,
subtly different snapshot of "now".
"""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.database.models.gameweek import Gameweek
from packages.domain.optimization import TargetGameweekStatus


def _actionable_gameweek_ids_ordered(
    session: Session,
    season_id: uuid.UUID,
    now: datetime,
    limit: int | None = None,
    max_fpl_event_id: int | None = None,
) -> list[uuid.UUID]:
    """Gameweek ids whose deadline hasn't passed, ascending by ``deadline_time``, optionally capped/bounded.

    A gameweek's deadline always precedes it finishing, so ``deadline_time > now``
    alone is sufficient to mean "still actionable" — no separate ``finished``
    check is needed.
    """
    stmt = select(Gameweek.id).where(Gameweek.season_id == season_id, Gameweek.deadline_time > now)
    if max_fpl_event_id is not None:
        stmt = stmt.where(Gameweek.fpl_event_id <= max_fpl_event_id)
    stmt = stmt.order_by(Gameweek.deadline_time.asc())
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.execute(stmt).scalars().all())


def resolve_target_gameweek(
    session: Session, season_id: uuid.UUID, now: datetime
) -> tuple[uuid.UUID | None, TargetGameweekStatus]:
    """Return the earliest still-actionable gameweek (by deadline) for a season, if any.

    Returns ``(None, NO_ACTIVE_GAMEWEEK)`` if every gameweek in the season
    has a passed deadline.
    """
    gameweek_ids = _actionable_gameweek_ids_ordered(session, season_id, now, limit=1)

    if not gameweek_ids:
        return None, TargetGameweekStatus.NO_ACTIVE_GAMEWEEK
    return gameweek_ids[0], TargetGameweekStatus.RESOLVED


def resolve_horizon_gameweeks(
    session: Session, season_id: uuid.UUID, horizon_length: int, now: datetime
) -> list[uuid.UUID]:
    """The earliest still-actionable gameweek plus up to ``horizon_length - 1`` more, by ascending deadline.

    Gracefully truncates near season end (returns fewer than
    ``horizon_length`` gameweek ids, never errors) — same philosophy as
    ``resolve_target_gameweek``'s ``NO_ACTIVE_GAMEWEEK`` handling. Returns an
    empty list if there are zero actionable gameweeks at all (the real
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
    return _actionable_gameweek_ids_ordered(session, season_id, now, limit=horizon_length)


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


def resolve_gameweeks_within_window(
    session: Session, season_id: uuid.UUID, stop_event: int, now: datetime
) -> list[uuid.UUID]:
    """Still-actionable gameweeks for a season whose ``fpl_event_id <= stop_event``, ascending by deadline.

    Same actionable-only, ascending-deadline ordering as
    ``resolve_target_gameweek``/``resolve_horizon_gameweeks`` — just bounded by
    a chip window's own ``stop_event`` (see ``app.chips.windows.ChipWindow``)
    instead of a fixed count, so a chip's currently-open window can be scanned
    EXHAUSTIVELY (every gameweek still eligible for that chip, no cap) rather
    than an arbitrary caller-supplied horizon length. Returns an empty list
    when there is nothing actionable left in the window (already-closed
    window, or the real off-season state) — callers must treat this as "no
    candidate gameweek to evaluate", not an error.
    """
    return _actionable_gameweek_ids_ordered(session, season_id, now, max_fpl_event_id=stop_event)
