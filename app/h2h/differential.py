"""Pure computation of a squad differential between two 15-man (or partial) squads.

The single most actionable piece of H2H intel a manager wants when looking
at a rival: which players do I own that they don't, and vice versa. No I/O,
no DB, no ORM objects -- takes two plain lists of player ids and returns a
set difference, so it's trivially unit-testable. Resolving those ids to
display names (``web_name``/position) is the caller's job (see
``apps.api.routers.leagues.list_league_rivals``), same "computation is pure,
persistence/resolution is the caller's" split ``app.h2h.rival_state`` already
established.
"""

import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class SquadDifferential:
    """``only_first``/``only_second`` are disjoint from each other by construction (each is a
    set-difference against the other side), but NOT necessarily from the shared/template
    players both squads own -- those simply don't appear in either set."""

    only_first: frozenset[uuid.UUID]
    only_second: frozenset[uuid.UUID]


def compute_squad_differential(
    first_squad_player_ids: list[uuid.UUID], second_squad_player_ids: list[uuid.UUID]
) -> SquadDifferential:
    first = frozenset(first_squad_player_ids)
    second = frozenset(second_squad_player_ids)
    return SquadDifferential(only_first=first - second, only_second=second - first)
