"""Concrete JobRunner implementations, one module per job type."""

from app.jobs.handlers import (
    chip_simulation,  # noqa: F401  (import registers the handler)
    data_ingestion,  # noqa: F401  (import registers the handler)
    feature_generation,  # noqa: F401  (import registers the handler)
    h2h_simulation,  # noqa: F401  (import registers the handler)
    historical_data_ingestion,  # noqa: F401  (import registers the handler)
    league_sync,  # noqa: F401  (import registers the handler)
    lineup_optimization,  # noqa: F401  (import registers the handler)
    manager_sync,  # noqa: F401  (import registers the handler)
    model_training,  # noqa: F401  (import registers the handler)
    rules_sync,  # noqa: F401  (import registers the handler)
    squad_optimization,  # noqa: F401  (import registers the handler)
    system_health,  # noqa: F401  (import registers the handler)
    transfer_optimization,  # noqa: F401  (import registers the handler)
)

__all__ = [
    "chip_simulation",
    "data_ingestion",
    "feature_generation",
    "h2h_simulation",
    "historical_data_ingestion",
    "league_sync",
    "lineup_optimization",
    "manager_sync",
    "model_training",
    "rules_sync",
    "squad_optimization",
    "system_health",
    "transfer_optimization",
]
