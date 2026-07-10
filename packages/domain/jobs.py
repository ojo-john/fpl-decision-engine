"""Job-related domain enums.

These enums define the closed set of job types, statuses, and trigger types
used throughout the FPL Decision Engine's job execution system. They are
intentionally framework-agnostic (no SQLAlchemy/FastAPI imports here).
"""

from enum import Enum


class JobType(str, Enum):
    """The full catalog of job types the system knows about.

    Only ``SYSTEM_HEALTH`` has a registered handler as of Milestone 0. The
    remaining values exist so the schema/enum surface is stable as later
    milestones add real handlers for them.
    """

    RULES_SYNC = "rules_sync"
    DATA_INGESTION = "data_ingestion"
    FEATURE_GENERATION = "feature_generation"
    MODEL_TRAINING = "model_training"
    PROJECTION_GENERATION = "projection_generation"
    SQUAD_OPTIMIZATION = "squad_optimization"
    LINEUP_OPTIMIZATION = "lineup_optimization"
    TRANSFER_OPTIMIZATION = "transfer_optimization"
    CHIP_SIMULATION = "chip_simulation"
    H2H_SIMULATION = "h2h_simulation"
    RECOMMENDATION_GENERATION = "recommendation_generation"
    GAMEWEEK_REVIEW = "gameweek_review"
    BACKTEST = "backtest"
    FULL_PIPELINE = "full_pipeline"
    SYSTEM_HEALTH = "system_health"


class JobStatus(str, Enum):
    """Lifecycle status of a job run."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class TriggerType(str, Enum):
    """How a job run was initiated."""

    LOCAL = "local"
    API = "api"
    WEB = "web"
    GITHUB_MANUAL = "github_manual"
    GITHUB_SCHEDULE = "github_schedule"
    RETRY = "retry"
