"""Per-job-run cache of empirical (actual - xP) residuals, used for Monte Carlo simulation.

Extracted verbatim (Milestone 6 Layer 1) from
``app.jobs.handlers.h2h_simulation`` -- pure code move, no behavior change --
so Milestone 6's chip-scenario Monte Carlo simulation (a later layer) can
reuse this same ~80-line class without duplicating it. See
``tests/integration/test_h2h_simulation_job.py`` for the proof this move is
behavior-preserving: every test there passed before and after the move,
unchanged.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.database.models.player import Player
from packages.database.models.player_gameweek_stats import PlayerGameweekStats
from packages.database.models.position import Position


class _ResidualCache:
    """Per-job-run cache of empirical (actual - xP) residuals: one query per player, one per position.

    Scoped to a single job run (never persisted/shared across runs) --
    residual pools are cheap to recompute and the whole point is to reflect
    the latest data at simulation time.

    ``for_position`` prefers a CURRENT-season, same-position pool, but falls
    back to an ALL-seasons, same-position pool (matched by
    ``Position.short_name``, since ``Position``/``Player`` rows -- and
    therefore ``position_id`` -- are season-scoped) when the current
    season's own pool is empty. This matters concretely at the start of a
    fresh season (the realistic state for a brand-new current season with no
    gameweeks played/ingested yet): without this fallback, EVERY player's
    residual sampling would silently degrade to zero noise (see
    ``app.h2h.simulate._sample_residual``'s final fallback) simply because
    the current season hasn't accumulated its own history yet, not because
    no relevant empirical data exists anywhere in the system.
    """

    def __init__(self, session: Session, season_id: uuid.UUID):
        self._session = session
        self._season_id = season_id
        self._by_player: dict[uuid.UUID, tuple[float, ...]] = {}
        self._by_position_current_season: dict[uuid.UUID, tuple[float, ...]] = {}
        self._by_position_all_seasons: dict[str, tuple[float, ...]] = {}

    def for_player(self, player_id: uuid.UUID) -> tuple[float, ...]:
        if player_id not in self._by_player:
            rows = self._session.execute(
                select(PlayerGameweekStats.total_points, PlayerGameweekStats.xp_estimate).where(
                    PlayerGameweekStats.player_id == player_id,
                    PlayerGameweekStats.xp_estimate.is_not(None),
                )
            ).all()
            self._by_player[player_id] = tuple(float(total) - float(xp) for total, xp in rows)
        return self._by_player[player_id]

    def for_position(
        self, position_id: uuid.UUID | None, position_short_name: str | None
    ) -> tuple[float, ...]:
        current_season_pool = self._current_season_pool(position_id) if position_id is not None else ()
        if current_season_pool:
            return current_season_pool
        if position_short_name is not None:
            return self._all_seasons_pool(position_short_name)
        return ()

    def _current_season_pool(self, position_id: uuid.UUID) -> tuple[float, ...]:
        if position_id not in self._by_position_current_season:
            rows = self._session.execute(
                select(PlayerGameweekStats.total_points, PlayerGameweekStats.xp_estimate)
                .join(Player, Player.id == PlayerGameweekStats.player_id)
                .where(
                    Player.season_id == self._season_id,
                    Player.position_id == position_id,
                    PlayerGameweekStats.xp_estimate.is_not(None),
                )
            ).all()
            self._by_position_current_season[position_id] = tuple(
                float(total) - float(xp) for total, xp in rows
            )
        return self._by_position_current_season[position_id]

    def _all_seasons_pool(self, position_short_name: str) -> tuple[float, ...]:
        if position_short_name not in self._by_position_all_seasons:
            rows = self._session.execute(
                select(PlayerGameweekStats.total_points, PlayerGameweekStats.xp_estimate)
                .join(Player, Player.id == PlayerGameweekStats.player_id)
                .join(Position, Position.id == Player.position_id)
                .where(
                    Position.short_name == position_short_name,
                    PlayerGameweekStats.xp_estimate.is_not(None),
                )
            ).all()
            self._by_position_all_seasons[position_short_name] = tuple(
                float(total) - float(xp) for total, xp in rows
            )
        return self._by_position_all_seasons[position_short_name]
