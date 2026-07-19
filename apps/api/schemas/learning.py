"""Pydantic request/response schemas for the /jobs/review-gameweek, /jobs/backtest, and /learning
endpoints.
"""

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
    # Milestone 7 Layer 6 -- see packages/database/models/learning_report.py's own docstring for
    # the exact "NULL until the retraining-trigger step actually runs" convention these follow.
    retraining_triggered: bool | None
    retraining_job_id: uuid.UUID | None
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


class BacktestRequest(BaseModel):
    # model_version_id is REQUIRED -- see app.jobs.handlers.backtest's own docstring for why there
    # is no sensible default (which model would "the" backtest even mean?).
    model_version_id: uuid.UUID
    requested_by: str | None = None


class BacktestRunOut(BaseModel):
    backtest_run_id: uuid.UUID
    model_version_id: uuid.UUID
    model_family: str
    feature_version: int
    run_status: str
    usable_seasons: list[str]
    fold_count: int
    degenerate_single_season: bool
    fold_metrics: list[dict[str, Any]]
    aggregate_mae: Decimal | None
    aggregate_rmse: Decimal | None
    aggregate_mean_bias: Decimal | None
    aggregate_spearman: Decimal | None
    aggregate_top_20_capture_rate: Decimal | None
    n_observations: int
    started_at: datetime
    completed_at: datetime
