"""Concrete JobRunner implementations, one module per job type."""

from app.jobs.handlers import (
    data_ingestion,  # noqa: F401  (import registers the handler)
    rules_sync,  # noqa: F401  (import registers the handler)
    system_health,  # noqa: F401  (import registers the handler)
)

__all__ = ["data_ingestion", "rules_sync", "system_health"]
