"""Ingestion/data-quality domain enums.

Framework-agnostic (no SQLAlchemy/FastAPI imports here), matching the style
of ``packages/domain/jobs.py``.
"""

from enum import Enum


class EndpointHealth(str, Enum):
    """Health classification of a single upstream FPL API endpoint fetch."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class IngestionRunType(str, Enum):
    """What kind of ingestion run this was."""

    RULES_SYNC = "rules_sync"
    DATA_INGESTION = "data_ingestion"
    HISTORICAL_DATA_INGESTION = "historical_data_ingestion"


class IngestionRunStatus(str, Enum):
    """Lifecycle status of an ingestion run."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    SUCCEEDED_WITH_WARNINGS = "succeeded_with_warnings"
    FAILED = "failed"


class DataQualitySeverity(str, Enum):
    """Severity of a single data quality issue."""

    CRITICAL = "critical"
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"
