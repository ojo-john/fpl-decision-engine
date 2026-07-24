"""Pydantic request/response schemas for the /optimize endpoints."""

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator


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


class OptimizationRunSummaryOut(BaseModel):
    """Slim per-row shape for GET "" list endpoints -- omits ``players`` (an N+1 join per
    row) since a history list only needs enough to pick a run to open, not its full XI.
    """

    optimization_run_id: uuid.UUID
    recommendation_type: str
    run_status: str
    squad_total_cost: int | None
    squad_projected_points: Decimal | None
    xi_projected_points: Decimal | None
    started_at: datetime
    completed_at: datetime | None


class OptimizationErrorResponse(BaseModel):
    error_code: str | None
    error_message: str | None
    job_id: str


class TransferOptimizationRequest(BaseModel):
    manager_squad_snapshot_id: uuid.UUID
    horizon_gameweeks: int = Field(default=6, ge=4, le=8)
    requested_by: str | None = None


class TransferMoveOut(BaseModel):
    player_out_id: uuid.UUID
    player_out_web_name: str | None
    player_in_id: uuid.UUID
    player_in_web_name: str | None
    player_out_selling_price: int
    player_in_cost: int
    projected_points_gain_over_horizon: Decimal


class TransferScenarioOut(BaseModel):
    transfers_count: int
    moves: list[TransferMoveOut]
    raw_horizon_points_gain: Decimal
    hit_cost: int
    net_horizon_points_gain: Decimal
    is_recommended: bool


class TransferOptimizationRunOut(BaseModel):
    transfer_optimization_run_id: uuid.UUID
    manager_squad_snapshot_id: uuid.UUID
    run_status: str
    horizon_gameweek_count: int
    free_transfers_available: int
    bank: int
    recommended_action: str  # "roll" | "one_transfer" | "two_transfers"
    scenarios: list[TransferScenarioOut]  # always 3, for transparency/comparison
    started_at: datetime
    completed_at: datetime | None


class TransferOptimizationRunSummaryOut(BaseModel):
    """Slim per-row shape for GET /transfers list -- omits ``scenarios``/``moves``."""

    transfer_optimization_run_id: uuid.UUID
    manager_squad_snapshot_id: uuid.UUID
    run_status: str
    recommended_action: str | None  # None for a run that never reached a recommendation
    started_at: datetime
    completed_at: datetime | None
