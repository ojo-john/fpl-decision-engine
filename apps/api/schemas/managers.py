"""Pydantic request/response schemas for the /managers and /squads endpoints."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, model_validator

SQUAD_SIZE = 15


class ManagerSyncRequest(BaseModel):
    fpl_manager_id: int
    requested_by: str | None = None


class ManualSquadRequest(BaseModel):
    """A hand-entered 15-player squad, with no external FPL manager behind it.

    ``fpl_element_ids`` must resolve to exactly 15 unique current-season
    ``Player`` rows — a STRICTER requirement than ``MANAGER_SYNC``'s
    tolerant "unresolved element" handling, deliberately: sync must tolerate
    uncontrollable external reality (a real manager's squad can reference a
    player who's been removed/renamed since), whereas manual entry is the
    user typing ids straight from our own ``/players`` list right now, so
    requiring resolvability here is a reasonable boundary check.
    """

    fpl_element_ids: list[int]
    bank: int = Field(ge=0)
    free_transfers_available: int = Field(ge=0)
    active_chip: str | None = None
    requested_by: str | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> "ManualSquadRequest":
        if len(self.fpl_element_ids) != SQUAD_SIZE or len(set(self.fpl_element_ids)) != SQUAD_SIZE:
            raise ValueError("fpl_element_ids must contain exactly 15 unique ids.")
        return self


class SquadPlayerOut(BaseModel):
    """One resolved squad member -- see apps.api.routers.managers._load_snapshot_response."""

    player_id: uuid.UUID
    web_name: str | None
    position: str | None
    now_cost: int


class ManagerSquadSnapshotOut(BaseModel):
    manager_squad_snapshot_id: uuid.UUID
    source: str
    fpl_manager_id: int | None
    is_current: bool
    bank: int
    free_transfers_available: int
    squad_player_ids: list[uuid.UUID]
    players: list[SquadPlayerOut]
    has_unresolved_players: bool
    unresolved_fpl_element_ids: list[int]
    created_at: datetime
