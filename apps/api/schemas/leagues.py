"""Pydantic response schemas for the /leagues endpoints (Milestone 8 Layer 1)."""

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class LeagueOut(BaseModel):
    """One synced H2H league -- lets a caller (apps/web) resolve a league_id before
    calling GET /leagues/{league_id}/rivals or /standings, since neither of those
    accepts the raw fpl_league_id. See packages.database.models.league."""

    league_id: uuid.UUID
    fpl_league_id: int
    name: str


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
