"""Pydantic response schemas for the /players endpoints."""

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.players.price_signal import PriceMomentum


class PlayerSummary(BaseModel):
    """One row in a /players list response.

    Built manually from a joined (Player, Team, Position) row rather than via
    ``from_attributes`` on a bare ``Player`` — see
    ``apps.api.routers.players.list_players``.
    """

    id: uuid.UUID
    fpl_element_id: int
    web_name: str | None
    team_id: uuid.UUID | None
    team_short_name: str | None
    position: str | None
    now_cost: int
    status: str | None
    total_points: int
    form: Decimal | None
    points_per_game: Decimal | None
    ep_next: Decimal | None
    is_quarantined: bool


class PlayerListResponse(BaseModel):
    items: list[PlayerSummary]
    page: int
    page_size: int
    total: int


class PlayerPriceMomentumOut(BaseModel):
    """See ``app.players.price_signal`` -- an explainable heuristic, not FPL's real algorithm."""

    momentum: PriceMomentum
    net_transfers_event: int | None
    estimated_owners: int | None
    momentum_ratio: Decimal | None


class PlayerDetail(BaseModel):
    """Full detail for a single player, matching the ``players`` table columns.

    Always includes ``is_quarantined``/``quarantine_reason`` regardless of
    the player's status — callers must not assume a returned player is
    necessarily eligible for optimization.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    fpl_element_id: int
    fpl_code: int | None
    season_id: uuid.UUID
    team_id: uuid.UUID | None
    team_short_name: str | None = None
    position_id: uuid.UUID | None
    position: str | None = None
    first_name: str | None
    second_name: str | None
    web_name: str | None
    now_cost: int
    status: str | None
    total_points: int
    minutes: int
    goals_scored: int
    assists: int
    clean_sheets: int
    goals_conceded: int
    own_goals: int
    penalties_saved: int
    penalties_missed: int
    yellow_cards: int
    red_cards: int
    saves: int
    bonus: int
    bps: int
    selected_by_percent: Decimal | None
    transfers_in_event: int | None
    transfers_out_event: int | None
    cost_change_event: int | None
    chance_of_playing_next_round: int | None
    chance_of_playing_this_round: int | None
    points_per_game: Decimal | None
    form: Decimal | None
    ep_this: Decimal | None
    ep_next: Decimal | None
    starts: int
    news: str | None
    is_quarantined: bool
    quarantine_reason: str | None
    # Not a Player column -- computed by the router via app.players.price_signal and attached
    # after model_validate (same pattern as team_short_name/position above), since it depends on
    # the season's total_players, not anything on the player row alone.
    price_momentum: PlayerPriceMomentumOut | None = None


class PlayerPriceChangeOut(BaseModel):
    """One OBSERVED ``now_cost`` change -- see ``packages.database.models.player_price_history``."""

    old_cost: int
    new_cost: int
    observed_at: datetime


class PlayerProjectionResponse(BaseModel):
    """The live output of ``app.optimization.projection.project_player`` for one player.

    The fields from ``is_cold_start_season`` onward are the "why this
    projection" breakdown (added post-review) -- the real inputs already
    computed behind ``projected_points``, not a new explainability system.
    See ``apps.api.routers.players.get_player_projection``.
    """

    player_id: uuid.UUID
    projected_points: float
    source: str
    is_eligible: bool
    projection_formula_version: int
    chance_of_playing_next_round: int | None
    status: str | None

    is_cold_start_season: bool
    # Echoes the two raw inputs app.optimization.projection._cold_start_projection blends when
    # is_cold_start_season is True -- None when not applicable (warm season, or no prior-season
    # history for a brand-new player) rather than a stale/misleading number.
    ep_next: Decimal | None
    career_avg_points_prior_seasons: Decimal | None
    next_fixture_difficulty: int | None
    # Set only when projected_points actually came from a champion model prediction (source
    # starts with "ml_champion_") -- None for every fallback/cold-start/no-champion path.
    champion_model_family: str | None
    champion_model_version_id: uuid.UUID | None
    explanation: str
