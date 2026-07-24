"""Pydantic request/response schemas for the /h2h endpoints."""

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class H2HSimulationRequest(BaseModel):
    manager_squad_snapshot_id: uuid.UUID
    rival_profile_id: uuid.UUID
    # Provenance only (which real h2h_matches.fpl_match_id this simulation checks, if any) --
    # see H2HSimulationRun's own docstring for why this is deliberately not a foreign key.
    h2h_match_fpl_id: int | None = None
    # Explicit override -- e.g. a past-completed-gameweek plausibility check. Omit to let the
    # job resolve the next unfinished gameweek, falling back to the most recently finished one
    # off-season (see app.jobs.handlers.h2h_simulation's own docstring).
    target_fpl_event_id: int | None = None
    requested_by: str | None = None


class H2HStrategyCandidateOut(BaseModel):
    """One named strategy candidate's simulated outcome (Milestone 5 Layer 4).

    See ``packages.database.models.h2h_strategy_candidate.H2HStrategyCandidate``
    for the full field rationale — ``transfer_optimization_run_scenario_id``/
    ``captain_player_id`` are ``None`` whenever that candidate reused
    EP_OPTIMAL's own squad/captain unchanged.
    """

    strategy_label: str
    win_probability: Decimal
    draw_probability: Decimal
    loss_probability: Decimal
    expected_margin: Decimal
    is_recommended: bool
    rationale: str | None
    transfer_optimization_run_scenario_id: uuid.UUID | None
    captain_player_id: uuid.UUID | None


class H2HSimulationRunOut(BaseModel):
    h2h_simulation_run_id: uuid.UUID
    run_status: str
    target_fpl_event_id: int
    simulation_method_version: int
    sample_count: int | None
    user_score_mean: Decimal | None
    user_score_floor: Decimal | None
    user_score_ceiling: Decimal | None
    rival_score_mean: Decimal | None
    rival_score_floor: Decimal | None
    rival_score_ceiling: Decimal | None
    win_probability: Decimal | None
    draw_probability: Decimal | None
    loss_probability: Decimal | None
    expected_margin: Decimal | None
    margin_std_dev: Decimal | None
    confidence: str | None
    # Milestone 5 Layer 4: risk-adjusted strategy comparison. recommended_strategy/
    # h2h_adjustment_materially_changed are None only for a run that predates Layer 4
    # (never for a freshly-SUCCEEDED run, which always populates both).
    recommended_strategy: str | None
    h2h_adjustment_materially_changed: bool | None
    strategy_candidates: list[H2HStrategyCandidateOut]
    started_at: datetime
    completed_at: datetime | None


class H2HSimulationRunSummaryOut(BaseModel):
    """Slim per-row shape for GET /h2h/simulate list -- omits ``strategy_candidates``."""

    h2h_simulation_run_id: uuid.UUID
    manager_squad_snapshot_id: uuid.UUID
    rival_profile_id: uuid.UUID
    run_status: str
    target_fpl_event_id: int
    win_probability: Decimal | None
    draw_probability: Decimal | None
    loss_probability: Decimal | None
    started_at: datetime
    completed_at: datetime | None


class H2HSimulationErrorResponse(BaseModel):
    error_code: str | None
    error_message: str | None
    job_id: str
