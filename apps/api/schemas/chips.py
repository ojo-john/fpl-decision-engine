"""Pydantic request/response schemas for the /optimize/chips endpoint."""

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class ChipSimulationRequest(BaseModel):
    manager_squad_snapshot_id: uuid.UUID
    # Omit to evaluate all 4 chips (WILDCARD/FREEHIT/BBOOST/3XC) -- see
    # app.jobs.handlers.chip_simulation._resolve_requested_chip_types.
    chip_types: list[str] | None = None
    requested_by: str | None = None


class ChipWindowCandidateOut(BaseModel):
    """One evaluated candidate gameweek for one chip type -- kept for full transparency, not just
    the recommended candidate (mirrors ``TransferOptimizationRunScenario``'s own "every evaluated
    scenario stays visible" convention).
    """

    candidate_fpl_event_id: int
    gain_mean: Decimal
    gain_floor: Decimal
    gain_ceiling: Decimal
    is_blank_gameweek: bool | None
    is_double_gameweek: bool | None
    rank_within_chip_type: int
    # Milestone 6 Layer 4 (league-context refinement): populated ONLY on the immediate candidate
    # of a run whose snapshot has a real, resolvable FPL manager id AND a real scheduled H2H
    # opponent for that exact gameweek -- None otherwise (every other candidate, or no real
    # opponent could be resolved at all). See app.jobs.handlers.chip_simulation's own docstring.
    h2h_win_probability_with_chip: Decimal | None = None
    h2h_win_probability_without_chip: Decimal | None = None


class ChipRecommendationOut(BaseModel):
    chip_type: str
    verdict: str
    rationale: str
    recommended_candidate: ChipWindowCandidateOut | None
    candidates: list[ChipWindowCandidateOut]


class ChipOptimizationRunOut(BaseModel):
    chip_optimization_run_id: uuid.UUID
    run_status: str
    target_fpl_event_id: int
    recommendations: list[ChipRecommendationOut]
    started_at: datetime
    completed_at: datetime | None


class ChipOptimizationRunSummaryOut(BaseModel):
    """Slim per-row shape for GET /optimize/chips list -- omits ``recommendations``/
    ``candidates`` (a two-level join per row)."""

    chip_optimization_run_id: uuid.UUID
    manager_squad_snapshot_id: uuid.UUID
    run_status: str
    target_fpl_event_id: int
    started_at: datetime
    completed_at: datetime | None


class ChipSimulationErrorResponse(BaseModel):
    error_code: str | None
    error_message: str | None
    job_id: str


class ChipScheduleOptimizationRequest(BaseModel):
    """See app.jobs.handlers.chip_schedule_optimization's own module docstring.

    ``chip_optimization_run_id`` is optional -- omit it to auto-resolve the most recently
    completed SUCCEEDED CHIP_SIMULATION run (mirrors this codebase's own established
    "auto-resolve if not given" convention, e.g. RECOMMENDATION_GENERATION's target-gameweek
    resolution)."""

    chip_optimization_run_id: uuid.UUID | None = None
    requested_by: str | None = None


class ChipScheduleAssignmentOut(BaseModel):
    """One chip type's final decision from the assignment solve -- ``assigned_fpl_event_id``/
    ``gain_mean`` are both ``None`` together when the solver determined this chip is not worth
    playing within the evaluated candidates (a real, valid outcome, not a partial/error state)."""

    chip_type: str
    window_number: int
    assigned_fpl_event_id: int | None
    gain_mean: Decimal | None


class ChipScheduleOptimizationRunOut(BaseModel):
    chip_schedule_optimization_run_id: uuid.UUID
    source_chip_optimization_run_id: uuid.UUID
    run_status: str
    total_expected_gain: Decimal | None
    assignments: list[ChipScheduleAssignmentOut]
    started_at: datetime
    completed_at: datetime | None


class ChipScheduleOptimizationRunSummaryOut(BaseModel):
    """Slim per-row shape for GET /optimize/chips/schedule list -- omits ``assignments`` (mirrors
    ``ChipOptimizationRunSummaryOut``'s own identical reasoning)."""

    chip_schedule_optimization_run_id: uuid.UUID
    source_chip_optimization_run_id: uuid.UUID
    manager_squad_snapshot_id: uuid.UUID
    run_status: str
    total_expected_gain: Decimal | None
    started_at: datetime
    completed_at: datetime | None


class ChipWindowOut(BaseModel):
    """One chip-scheduling window (a chip typically gets 2 per season) -- see
    ``app.chips.windows.resolve_chip_windows``. Genuinely available pre-season, unlike
    ``ChipRecommendationOut`` (needs a real squad and passes through the optimization gate):
    FPL publishes these windows in ``bootstrap-static`` before a ball is kicked.
    """

    chip_type: str
    window_number: int
    start_event: int
    stop_event: int
    start_deadline: datetime | None
    stop_deadline: datetime | None
    # "past" | "open" | "upcoming", relative to the season's current gameweek -- "upcoming" for
    # every window, always, when there's no current gameweek yet (pre-season).
    status: str
