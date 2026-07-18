"""Pure DB-read primitives resolving real, already-known player outcomes (Milestone 7 Layer 2).

Every function here does exactly one thing: read ``player_gameweek_stats`` and
sum/report already-known real points. No business logic about WHAT is being
compared (captain doubling, chip effects, "recommended vs. actual" framing,
etc.) lives here -- that is ``app.learning.counterfactuals``'/
``app.learning.outcome_ingestion``'s job. This mirrors this codebase's
existing separation between a pure computation module and its caller's
resolution logic (e.g. ``app.h2h.simulate`` vs.
``app.jobs.handlers.h2h_simulation``), just with the DB read on this side of
the line instead of the caller's.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from packages.database.models.player_gameweek_stats import PlayerGameweekStats

__all__ = ["sum_actual_points"]


def sum_actual_points(session: Session, player_ids: list[uuid.UUID], gameweek_id: uuid.UUID) -> float:
    """Sum of ``player_gameweek_stats.total_points`` for these players in this gameweek.

    A player with NO row for this gameweek (didn't play, was never resolved,
    or is genuinely unresolvable) contributes 0 -- matching this codebase's
    established "no row = no points scored" convention already used
    elsewhere for absent ``player_gameweek_stats`` (e.g.
    ``app.optimization.residual_cache``'s own history lookups). A player with
    a Double Gameweek (2 rows, one per fixture -- see that model's own
    docstring) has BOTH rows' ``total_points`` summed, which is the correct
    real-world total for that gameweek.

    An empty ``player_ids`` list returns ``0.0`` rather than issuing a
    pointless query.
    """
    if not player_ids:
        return 0.0
    total = session.execute(
        select(func.coalesce(func.sum(PlayerGameweekStats.total_points), 0)).where(
            PlayerGameweekStats.player_id.in_(player_ids),
            PlayerGameweekStats.gameweek_id == gameweek_id,
        )
    ).scalar_one()
    return float(total)
