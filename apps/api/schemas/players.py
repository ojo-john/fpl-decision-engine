"""Pydantic response schemas for the /players endpoints."""

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.players.price_signal import PriceMomentum


class PlayerPriceMomentumOut(BaseModel):
    """See ``app.players.price_signal`` -- an explainable heuristic, not FPL's real algorithm."""

    momentum: PriceMomentum
    net_transfers_event: int | None
    estimated_owners: int | None
    momentum_ratio: Decimal | None


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
    # Same heuristic as PlayerDetail.price_momentum, computed once per request against the
    # active PriceSignalCalibration (see apps.api.routers.players.list_players) -- surfaced
    # here too so a manager scanning the whole list doesn't have to open each player individually
    # to see who's trending before locking in a squad.
    price_momentum: PlayerPriceMomentumOut | None = None


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
    # Distinct from is_cold_start_season above: True whenever THIS player individually has zero
    # player_gameweek_stats rows of their own this season (a late debut, a return from long-term
    # injury, ...), even if the season overall has moved on and is_cold_start_season is False --
    # see app.optimization.projection._is_cold_start_player. is_cold_start_season True always
    # implies this is also True (zero rows for everyone means zero for this player too); the
    # reverse is not true, which is exactly why both fields are surfaced separately rather than
    # collapsed into one.
    is_cold_start_player: bool
    # Echoes the raw inputs app.optimization.projection._cold_start_projection blends when
    # is_cold_start_season or is_cold_start_player is True -- None when not applicable (warm
    # player in a warm season, or no prior-season history for a brand-new player) rather than a
    # stale/misleading number.
    ep_next: Decimal | None
    # The plain full-season average, always shown for reference even when the blend actually
    # used the early-season-specific average instead (see career_avg_early_season_points_prior_seasons).
    career_avg_points_prior_seasons: Decimal | None
    # Average points across only each prior season's first few gameweeks (see
    # app.features.engineering.career_avg_early_season_points_prior_seasons) -- _cold_start_projection
    # prefers THIS over the plain full-season average whenever it exists, since a player's season-opening
    # form can differ meaningfully from their season-long average (well-known slow/fast starters).
    # None when the player has no early-season history across any prior season.
    career_avg_early_season_points_prior_seasons: Decimal | None
    next_fixture_difficulty: int | None
    # Set only when projected_points actually came from a champion model prediction (source
    # starts with "ml_champion_") -- None for every fallback/cold-start/no-champion path.
    champion_model_family: str | None
    champion_model_version_id: uuid.UUID | None
    # Set only when a player_availability_risk_notes row is active for this player and gameweek --
    # see app.optimization.projection.get_active_availability_risk_note. Distinct from FPL's own
    # official chance_of_playing_next_round-driven scaling (always reflected in `source` via
    # "_availability_scaled"): this is judgment-based, pre-official risk a human/research agent
    # flagged (e.g. a player short on match fitness after a long international tournament).
    availability_risk_reason: str | None = None
    availability_risk_severity: Decimal | None = None
    # Set only when a player_role_notes row is active for this player and gameweek -- see
    # app.optimization.projection.get_active_role_boost_note. A separate, independent mechanism
    # from availability_risk_* above (a boost, not a discount -- both can be active at once and
    # both apply, see that function's own docstring for why they're kept as two mechanisms).
    role_boost_reason: str | None = None
    role_boost_severity: Decimal | None = None
    explanation: str


class AvailabilityRiskNoteCreate(BaseModel):
    """Body for ``POST /players/{player_id}/availability-risk-notes``."""

    reason: str
    # Projection multiplier in (0, 1] -- e.g. 0.65 = discount to 65% of what the projection would
    # otherwise be. Validated at the boundary (see the router), not re-validated internally.
    severity: Decimal
    valid_from_fpl_event_id: int | None = None
    valid_until_fpl_event_id: int | None = None
    created_by: str = "manual"


class AvailabilityRiskNoteOut(BaseModel):
    """See ``packages.database.models.player_availability_risk_note``."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    player_id: uuid.UUID
    reason: str
    severity: Decimal
    valid_from_fpl_event_id: int | None
    valid_until_fpl_event_id: int | None
    created_at: datetime
    created_by: str


class RoleNoteCreate(BaseModel):
    """Body for ``POST /players/{player_id}/role-notes``."""

    role: str
    reason: str
    # Projection multiplier -- normally > 1.0 (a boost), e.g. 1.15 = increase to 115% of what the
    # projection would otherwise be. Not hard-validated at the boundary (unlike
    # AvailabilityRiskNoteCreate's own (0, 1] convention) -- see
    # packages.database.models.player_role_note's own column comment for why this stays generic.
    severity: Decimal
    valid_from_fpl_event_id: int | None = None
    valid_until_fpl_event_id: int | None = None
    created_by: str = "manual"


class RoleNoteOut(BaseModel):
    """See ``packages.database.models.player_role_note``."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    player_id: uuid.UUID
    role: str
    reason: str
    severity: Decimal
    valid_from_fpl_event_id: int | None
    valid_until_fpl_event_id: int | None
    created_at: datetime
    created_by: str


class ActiveNoteOut(BaseModel):
    """One row in ``GET /players/notes/active`` -- every currently-active
    ``player_availability_risk_notes``/``player_role_notes`` row across every player, for the
    CURRENT/next unfinished gameweek (not player-scoped like the existing per-player note
    endpoints). Built for external, read-only consumers that can't reach this app's database
    directly (e.g. ``.github/workflows/scout-watch.yml``'s dedup step) to know what's already
    known before proposing something as new.

    ``category`` is ``"availability_risk"`` or ``"role_boost"`` -- a plain str, not an enum,
    mirroring ``player_role_notes.role``'s own "nothing branches on the exact string" reasoning.
    ``role`` is only ever set for ``role_boost`` rows (``None`` for ``availability_risk``, which
    has no equivalent column).
    """

    player_id: uuid.UUID
    web_name: str | None
    category: str
    role: str | None
    reason: str
    severity: Decimal
    valid_from_fpl_event_id: int | None
    valid_until_fpl_event_id: int | None
    created_at: datetime
    created_by: str
