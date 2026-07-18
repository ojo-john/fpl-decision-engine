"""Pydantic request/response schemas for the /jobs/review-gameweek and /learning endpoints."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel


class GameweekReviewRequest(BaseModel):
    # target_fpl_event_id is REQUIRED -- see app.jobs.handlers.gameweek_review's own docstring for
    # why GAMEWEEK_REVIEW never auto-resolves a target gameweek the way the forward-looking jobs do.
    target_fpl_event_id: int
    season_id: uuid.UUID | None = None
    requested_by: str | None = None


class LearningReportOut(BaseModel):
    learning_report_id: uuid.UUID
    season_id: uuid.UUID
    gameweek_id: uuid.UUID
    target_fpl_event_id: int
    run_status: str
    recommendation_outcome_id: uuid.UUID | None
    failure_attribution_count: int | None
    learning_signals_computed: dict[str, Any] | None
    report_markdown: str | None
    summary: dict[str, Any] | None
    notable_findings: list[dict[str, Any]] | None
    started_at: datetime
    completed_at: datetime | None


class LearningReportErrorResponse(BaseModel):
    error_code: str | None
    error_message: str | None
    job_id: str


class LearningSignalOut(BaseModel):
    learning_signal_id: uuid.UUID
    season_id: uuid.UUID
    as_of_gameweek_id: uuid.UUID
    signal_type: str
    window_size_gameweeks: int
    value: Decimal
    sample_count: int
    threshold_breached: bool
    threshold_config_snapshot: dict[str, Any] | None
    computed_at: datetime


class FailureAttributionOut(BaseModel):
    failure_attribution_id: uuid.UUID
    recommendation_outcome_id: uuid.UUID
    reason_code: str
    category: str
    severity: Decimal | None
    evidence: dict[str, Any] | None
    created_at: datetime
