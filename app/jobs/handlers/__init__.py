"""Concrete JobRunner implementations, one module per job type."""

from app.jobs.handlers import (
    data_ingestion,  # noqa: F401  (import registers the handler)
    feature_generation,  # noqa: F401  (import registers the handler)
    historical_data_ingestion,  # noqa: F401  (import registers the handler)
    lineup_optimization,  # noqa: F401  (import registers the handler)
    rules_sync,  # noqa: F401  (import registers the handler)
    squad_optimization,  # noqa: F401  (import registers the handler)
    system_health,  # noqa: F401  (import registers the handler)
)

__all__ = [
    "data_ingestion",
    "feature_generation",
    "historical_data_ingestion",
    "lineup_optimization",
    "rules_sync",
    "squad_optimization",
    "system_health",
]
