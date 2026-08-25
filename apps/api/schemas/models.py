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


class ModelTrainingRequest(BaseModel):
    """See app.jobs.handlers.model_training's own module docstring.

    ``requested_by`` mirrors every other job-trigger request schema's own optional
    "who asked for this" field (e.g. BacktestRequest)."""

    feature_version: int | None = None
    requested_by: str | None = None


class ModelTrainingOut(BaseModel):
    """See app.jobs.handlers.model_training's own module docstring for ``promotion_outcome``'s two
    possible values: ``"champion"`` (this run's winner is now live -- either a true bootstrap, no
    champion existed before, or a self-heal of a previously-flagged champion whose artifact was
    missing/unusable) or ``"shadow_challenger"`` (a real, usable champion already exists; this run's
    winner is queued for real, retrospective shadow evaluation instead -- see
    app.modeling.promotion)."""

    feature_version: int
    usable_seasons: list[str]
    fold_count: int
    degenerate_single_season: bool
    champion_model_version_id: uuid.UUID
    champion_model_family: str
    promotion_outcome: str
    lifecycle_state: str


class RemoteTrainingDispatchOut(BaseModel):
    """See app.integrations.github_actions's own module docstring -- the FPL dashboard's own
    "Retrain model" button. ``workflow_runs_url`` is a real, followable link to the workflow's own
    Actions page (GitHub's workflow_dispatch API itself returns no run id to correlate against),
    not a specific run."""

    dispatched: bool
    workflow_runs_url: str


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
