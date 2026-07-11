"""The basic per-player points projection formula used by Milestone 2's optimizers.

No dependency on optimizer internals — Milestone 3 will later swap in real
model-generated projections behind this exact same ``PlayerProjection``
shape, so this stays a small, pure, deterministic function.
"""

import uuid
from dataclasses import dataclass

from packages.database.models.player import Player

PROJECTION_FORMULA_VERSION = 1

# Injured / suspended / unavailable — never sound advice to recommend, so
# these statuses get no decision variable at all in the optimizer's
# candidate pool (hard exclusion, not a down-weight).
_HARD_EXCLUDE_STATUSES = {"i", "s", "u"}

# chance_of_playing_next_round (%) below which we down-weight the projection.
# Exactly 75 is NOT scaled (only strictly below it) — a player is presumed
# fit enough at 75% to be worth full consideration.
_AVAILABILITY_SCALE_THRESHOLD = 75


@dataclass(frozen=True)
class PlayerProjection:
    player_id: uuid.UUID
    projected_points: float
    source: str
    is_eligible: bool


def project_player(player: Player) -> PlayerProjection:
    """Project a single player's expected points for the target gameweek.

    ``ep_next`` (FPL's own forward-looking estimate) is preferred over
    ``points_per_game`` (backward-looking season average) as the primary
    source; ``points_per_game`` is the fallback when ``ep_next`` is null.
    Statuses ``i``/``s``/``u`` are hard-excluded (``is_eligible=False``,
    ``projected_points=0.0``) regardless of any other field. Statuses ``d``
    (doubtful) and ``a`` (available) are never hard-excluded — only
    down-weighted via the availability multiplier below the 75% threshold.
    """
    if player.status in _HARD_EXCLUDE_STATUSES:
        return PlayerProjection(player.id, 0.0, "unavailable_status_excluded", is_eligible=False)

    chance = player.chance_of_playing_next_round
    multiplier = (chance / 100) if chance is not None and chance < _AVAILABILITY_SCALE_THRESHOLD else 1.0

    if player.ep_next is not None:
        base = float(player.ep_next)
        source = "ep_next_availability_scaled" if multiplier < 1.0 else "ep_next"
    elif player.points_per_game is not None:
        base = float(player.points_per_game)
        source = "points_per_game_availability_scaled" if multiplier < 1.0 else "points_per_game"
    else:
        return PlayerProjection(player.id, 0.0, "no_data_zero", is_eligible=True)

    return PlayerProjection(player.id, base * multiplier, source, is_eligible=True)
