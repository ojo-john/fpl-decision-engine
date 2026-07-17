"""Learning-loop domain enums (Milestone 7 Layer 1: Recommendation Correlation/Audit).

Framework-agnostic (no SQLAlchemy/FastAPI imports here), matching the style of
``packages/domain/jobs.py``/``packages/domain/rules.py``/``packages/domain/chips.py``.

Deliberately scoped to exactly what Layer 1 needs. Later Milestone 7 layers
(counterfactual comparison types, failure-reason codes, etc.) get their own
enums added here when THEY are built, not speculatively added now.
"""

from enum import Enum


class RecommendationConfidence(str, Enum):
    """How much a ``recommendation_audits`` row's own generation context should be trusted.

    See ``app.jobs.handlers.recommendation_generation.compute_confidence`` for
    the exact, named-constant thresholds that assign this.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class RecommendationGenerationRunStatus(str, Enum):
    """Lifecycle status of a RECOMMENDATION_GENERATION job run.

    Kept for symmetry with every other job type's own run-status enum (e.g.
    ``TransferOptimizationRunStatus``/``ChipOptimizationRunStatus``) even
    though ``recommendation_audits`` itself has no ``run_status`` column --
    see that job handler's module docstring for why this job's own
    "build fully in memory, insert once" discipline means a row only ever
    represents a complete SUCCEEDED outcome (mirrors
    ``app.jobs.handlers.manager_sync``'s precedent), so this enum's
    ``RUNNING``/``FAILED`` members are never actually persisted anywhere --
    only ``JobResult.status`` (``packages.domain.jobs.JobStatus``) reflects
    a FAILED outcome for this job type.
    """

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
