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


class ChipSimulationErrorResponse(BaseModel):
    error_code: str | None
    error_message: str | None
    job_id: str
