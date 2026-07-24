"""Pydantic response schemas for the /leagues endpoints (Milestone 8 Layer 1)."""

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class LeagueSyncRequest(BaseModel):
    fpl_league_id: int
    # The calling user's own FPL manager id, if provided -- excluded from
    # being synced as a "rival" of themselves (see
    # app.jobs.handlers.league_sync's own "self exclusion is optional" note).
    exclude_fpl_manager_id: int | None = None
    requested_by: str | None = None


class LeagueSyncRunOut(BaseModel):
    """See packages.database.models.league_sync_run.LeagueSyncRun."""

    league_sync_run_id: uuid.UUID
    league_id: uuid.UUID | None
    run_status: str
    rivals_synced_count: int | None
    rivals_unavailable_count: int | None
    started_at: datetime
    completed_at: datetime | None


class LeagueOut(BaseModel):
    """One synced H2H league -- lets a caller (apps/web) resolve a league_id before
    calling GET /leagues/{league_id}/rivals or /standings, since neither of those
    accepts the raw fpl_league_id. See packages.database.models.league."""

    league_id: uuid.UUID
    fpl_league_id: int
    name: str
    is_priority: bool


class SquadDifferentialPlayerOut(BaseModel):
    """One player in a squad differential -- see app.h2h.differential.compute_squad_differential."""

    player_id: uuid.UUID
    web_name: str | None
    position: str | None


class RivalProfileOut(BaseModel):
    """One rival's DERIVED + PREDICTED state.

    Mirrors ``app.h2h.rival_state.RivalStateResult`` field-for-field -- see
    that module's own docstring for the full confidence-assignment and
    cold-start-handling rationale.
    """

    rival_profile_id: uuid.UUID
    fpl_manager_id: int
    last_known_snapshot_id: uuid.UUID | None
    derived_free_transfers_min: int | None
    derived_free_transfers_max: int | None
    derived_free_transfers_confidence: str | None
    derived_likely_remaining_chips: list[str]
    predicted_no_transfer_probability: Decimal | None
    predicted_one_transfer_probability: Decimal | None
    predicted_multi_transfer_probability: Decimal | None
    predicted_hit_probability: Decimal | None
    predicted_captain_probabilities: dict
    predicted_likely_transfer_targets: list
    tendency_features: dict
    unknowns: list[str]
    evidence: dict
    overall_confidence: str
    computed_at: datetime
    # Both None together when a differential can't be computed: no current-season
    # MANAGER_SYNC/manual squad exists yet, or this rival has no AVAILABLE picks
    # snapshot yet (last_known_snapshot_id is None). Never partially populated.
    players_only_you_have: list[SquadDifferentialPlayerOut] | None
    players_only_rival_has: list[SquadDifferentialPlayerOut] | None


class LeagueEntryOut(BaseModel):
    """One (league, manager) standings row -- see packages.database.models.league_entry."""

    fpl_manager_id: int
    player_name: str | None
    entry_name: str | None
    rank: int | None
    last_rank: int | None
    matches_played: int | None
    matches_won: int | None
    matches_drawn: int | None
    matches_lost: int | None
    points_for: int | None
    total: int | None
