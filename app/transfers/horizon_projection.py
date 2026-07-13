"""Horizon-summed points projection: Milestone 4 Layer 2's only extension over Milestone 3's
single-gameweek ``project_player_with_champion``.

Deliberately a thin, dumb wrapper — champion-vs-fallback selection, hard
exclusion, and per-player graceful degradation are all already handled
correctly, once per gameweek, by ``project_player_with_champion`` itself
(see ``app.optimization.projection``). This module's entire job is summing
that same call's output across every gameweek in a horizon; it must never
reimplement any part of that per-gameweek logic.

Callers are responsible for calling this once per candidate player and
reusing the result across every transfer scenario evaluated — never
recomputing it per scenario (see ``app.transfers.candidates``, which
precomputes exactly this way before any scenario search runs).
"""

import uuid

from sqlalchemy.orm import Session

from app.optimization.champion_loader import LoadedChampion
from app.optimization.projection import project_player_with_champion
from packages.database.models.player import Player


def compute_horizon_points(
    session: Session,
    player: Player,
    horizon_gameweek_ids: list[uuid.UUID],
    champion: LoadedChampion | None,
) -> float:
    """Sum of ``project_player_with_champion(...).projected_points`` across every gameweek in the horizon.

    Returns ``0.0`` for an empty horizon (nothing to sum) — this can happen
    near season end / off-season, where ``resolve_horizon_gameweeks`` itself
    gracefully returns fewer gameweeks, or none at all.
    """
    return sum(
        project_player_with_champion(session, player, gameweek_id, champion).projected_points
        for gameweek_id in horizon_gameweek_ids
    )
