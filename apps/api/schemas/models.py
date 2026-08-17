"""Pydantic response schemas for the /models endpoints (Milestone 8)."""

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class ChampionModelOut(BaseModel):
    """See packages.database.models.model_version.ModelVersion.is_champion."""

    model_version_id: uuid.UUID
    model_name: str
    model_family: str
    feature_version: int
    trained_at: datetime


class StartProbabilityTrainingRequest(BaseModel):
    """See app.jobs.handlers.start_probability_training's own module docstring.

    ``requested_by`` mirrors every other job-trigger request schema's own optional
    "who asked for this" field (e.g. BacktestRequest)."""

    feature_version: int | None = None
    requested_by: str | None = None


class StartProbabilityModelOut(BaseModel):
    """See packages.database.models.start_probability_model.StartProbabilityModel.

    ``is_active`` is always ``False`` on a just-trained row -- see that model's own docstring for
    why activation is a deliberate, separate, human-reviewed decision this schema surfaces the
    real evidence for (``aggregate_*``) but does not itself trigger."""

    start_probability_model_id: uuid.UUID
    feature_version: int
    target_variable: str
    usable_seasons: list[str]
    fold_count: int
    degenerate_single_season: bool
    aggregate_auc: Decimal | None
    aggregate_accuracy: Decimal | None
    aggregate_f1: Decimal | None
    aggregate_log_loss: Decimal | None
    n_observations: int
    is_active: bool
    trained_at: datetime
