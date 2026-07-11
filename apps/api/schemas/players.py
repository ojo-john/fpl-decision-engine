"""Pydantic response schemas for the /players endpoints."""

import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class PlayerSummary(BaseModel):
    """One row in a /players list response.

    Built manually from a joined (Player, Team, Position) row rather than via
    ``from_attributes`` on a bare ``Player`` — see
    ``apps.api.routers.players.list_players``.
    """

    id: uuid.UUID
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
    position_id: uuid.UUID | None
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


class PlayerProjectionResponse(BaseModel):
    """The live output of ``app.optimization.projection.project_player`` for one player."""

    player_id: uuid.UUID
    projected_points: float
    source: str
    is_eligible: bool
    projection_formula_version: int
    chance_of_playing_next_round: int | None
    status: str | None
