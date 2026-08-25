"""Pydantic request/response schemas for the /jobs/review-gameweek, /jobs/backtest,
/jobs/cold-start-validation, and /learning endpoints.
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
    # Milestone 7 Layer 6, revised 2026-08-25 -- see packages/database/models/learning_report.py's
    # own docstring: retraining_triggered now means "an MAE-breach push alert was sent", not "a
    # MODEL_TRAINING job was launched" -- retraining_job_id is always NULL for a new row going
    # forward (retraining is a manual, human-triggered decision now, never run from this step).
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


class ColdStartValidationRequest(BaseModel):
    # All optional -- see app.jobs.handlers.cold_start_projection_validation's own docstring for why
    # this job, unlike BACKTEST, has a fully sensible default (its own module-level candidate sets).
    half_life_candidates: list[float] | None = None
    difficulty_coefficient_candidates: list[float] | None = None
    blend_ratio_candidates: list[float] | None = None
    requested_by: str | None = None


class ColdStartValidationRunOut(BaseModel):
    cold_start_validation_run_id: uuid.UUID
    run_status: str
    early_season_gameweek_cutoff: int
    component_evaluated: str
    usable_seasons: list[str]
    seasons_evaluated: list[str]
    n_observations: int
    half_life_sweep: list[dict[str, Any]]
    default_half_life_seasons: Decimal
    default_half_life_mae: Decimal | None
    best_half_life_seasons: Decimal
    best_half_life_label: str
    best_half_life_mae: Decimal
    difficulty_coefficient_sweep: list[dict[str, Any]]
    default_difficulty_coefficient: Decimal
    default_difficulty_coefficient_mae: Decimal | None
    best_difficulty_coefficient: Decimal | None
    best_difficulty_coefficient_mae: Decimal | None
    blend_ratio_sweep: list[dict[str, Any]]
    default_blend_career_avg_weight: Decimal
    default_blend_mae: Decimal | None
    best_blend_career_avg_weight: Decimal | None
    best_blend_mae: Decimal | None
    blend_ratio_caveat: str
    started_at: datetime
    completed_at: datetime
