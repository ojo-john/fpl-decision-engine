"""Pydantic request/response schemas for the /optimize endpoints."""

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, model_validator


class InitialSquadRequest(BaseModel):
    requested_by: str | None = None


class LineupRequest(BaseModel):
    """Exactly one of ``source_optimization_run_id``/``player_ids`` must be set.

    ``player_ids`` is the only realistic input mode until manager-squad sync
    exists (a later milestone); ``source_optimization_run_id`` chains off a
    prior SUCCEEDED initial-squad recommendation.
    """

    source_optimization_run_id: uuid.UUID | None = None
    player_ids: list[uuid.UUID] | None = None
    requested_by: str | None = None

    @model_validator(mode="after")
    def _check_exactly_one_source(self) -> "LineupRequest":
        has_source = self.source_optimization_run_id is not None
        has_player_ids = self.player_ids is not None
        if has_source == has_player_ids:
            raise ValueError("Exactly one of 'source_optimization_run_id' or 'player_ids' must be provided.")
        return self


class OptimizationRunPlayerOut(BaseModel):
    player_id: uuid.UUID
    web_name: str | None
    position: str | None
    team_id: uuid.UUID | None
    is_starting: bool
    bench_order: int | None
    is_captain: bool
    is_vice_captain: bool
    projected_points: Decimal
    projection_source: str
    now_cost_snapshot: int


class OptimizationRunOut(BaseModel):
    optimization_run_id: uuid.UUID
    recommendation_type: str
    run_status: str
    solver_status: str | None
    target_gameweek_status: str
    squad_total_cost: int | None
    squad_projected_points: Decimal | None
    xi_projected_points: Decimal | None
    captain_player_id: uuid.UUID | None
    vice_captain_player_id: uuid.UUID | None
    players: list[OptimizationRunPlayerOut]
    started_at: datetime
    completed_at: datetime | None


class OptimizationErrorResponse(BaseModel):
    error_code: str | None
    error_message: str | None
    job_id: str
