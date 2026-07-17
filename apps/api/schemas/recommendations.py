"""Pydantic request/response schemas for the /recommendations endpoints."""

import uuid
from datetime import datetime

from pydantic import BaseModel


class RecommendationGenerationRequest(BaseModel):
    # Explicit overrides -- both optional. Omitting manager_squad_snapshot_id resolves the
    # current season via Season.is_current and leaves every manager-scoped component lookup
    # unscoped; omitting target_fpl_event_id resolves the next unfinished gameweek (hard
    # refusing off-season). See app.jobs.handlers.recommendation_generation's own docstring.
    manager_squad_snapshot_id: uuid.UUID | None = None
    target_fpl_event_id: int | None = None
    requested_by: str | None = None


class RecommendationComponentSummaryOut(BaseModel):
    """A compact per-run summary of one correlated component run -- deliberately NOT a full
    re-embedding of everything that component's own dedicated endpoint already exposes (e.g.
    ``/optimize/chips``'s full per-chip candidate breakdown) -- just enough to identify and
    briefly describe the correlated run; a caller who needs the full detail follows the id to
    that component's own endpoint.
    """

    run_id: uuid.UUID
    summary: str


class RecommendationAuditOut(BaseModel):
    recommendation_audit_id: uuid.UUID
    season_id: uuid.UUID
    target_gameweek_id: uuid.UUID
    target_fpl_event_id: int
    is_authoritative: bool
    confidence: str
    confidence_drivers: list[str]
    assumptions: list[str]
    known_risks: list[str]
    champion_model_version_id: uuid.UUID | None
    fpl_api_health_at_generation: str | None
    optimization: RecommendationComponentSummaryOut | None
    transfer_optimization: RecommendationComponentSummaryOut | None
    h2h_simulation: RecommendationComponentSummaryOut | None
    chip_optimization: RecommendationComponentSummaryOut | None
    generated_at: datetime


class RecommendationGenerationErrorResponse(BaseModel):
    error_code: str | None
    error_message: str | None
    job_id: str
