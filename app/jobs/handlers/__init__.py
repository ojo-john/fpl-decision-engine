"""Concrete JobRunner implementations, one module per job type."""

from app.jobs.handlers import (
    backtest,  # noqa: F401  (import registers the handler)
    chip_schedule_optimization,  # noqa: F401  (import registers the handler)
    chip_simulation,  # noqa: F401  (import registers the handler)
    cold_start_projection_validation,  # noqa: F401  (import registers the handler)
    data_ingestion,  # noqa: F401  (import registers the handler)
    deadline_reminder,  # noqa: F401  (import registers the handler)
    feature_generation,  # noqa: F401  (import registers the handler)
    full_pipeline,  # noqa: F401  (import registers the handler)
    gameweek_review,  # noqa: F401  (import registers the handler)
    h2h_simulation,  # noqa: F401  (import registers the handler)
    historical_data_ingestion,  # noqa: F401  (import registers the handler)
    league_sync,  # noqa: F401  (import registers the handler)
    lineup_optimization,  # noqa: F401  (import registers the handler)
    manager_sync,  # noqa: F401  (import registers the handler)
    model_training,  # noqa: F401  (import registers the handler)
    player_alert_check,  # noqa: F401  (import registers the handler)
    position_segmentation_validation,  # noqa: F401  (import registers the handler)
    recommendation_generation,  # noqa: F401  (import registers the handler)
    rules_sync,  # noqa: F401  (import registers the handler)
    squad_optimization,  # noqa: F401  (import registers the handler)
    stale_job_reconciliation,  # noqa: F401  (import registers the handler)
    start_probability_training,  # noqa: F401  (import registers the handler)
    system_health,  # noqa: F401  (import registers the handler)
    team_strength_sync,  # noqa: F401  (import registers the handler)
    transfer_optimization,  # noqa: F401  (import registers the handler)
)

__all__ = [
    "backtest",
    "chip_schedule_optimization",
    "chip_simulation",
    "cold_start_projection_validation",
    "data_ingestion",
    "deadline_reminder",
    "feature_generation",
    "full_pipeline",
    "gameweek_review",
    "h2h_simulation",
    "historical_data_ingestion",
    "league_sync",
    "lineup_optimization",
    "manager_sync",
    "model_training",
    "player_alert_check",
    "position_segmentation_validation",
    "recommendation_generation",
    "rules_sync",
    "squad_optimization",
    "stale_job_reconciliation",
    "start_probability_training",
    "system_health",
    "team_strength_sync",
    "transfer_optimization",
]
