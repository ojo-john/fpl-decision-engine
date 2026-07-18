"""Learning-loop domain enums (Milestone 7 Layer 1: Recommendation Correlation/Audit).

Framework-agnostic (no SQLAlchemy/FastAPI imports here), matching the style of
``packages/domain/jobs.py``/``packages/domain/rules.py``/``packages/domain/chips.py``.

Deliberately scoped to exactly what Layer 1 needs. Later Milestone 7 layers
(counterfactual comparison types, failure-reason codes, etc.) get their own
enums added here when THEY are built, not speculatively added now.

Milestone 7 Layer 2 (Recursive Learning Loop: Outcome Ingestion/Counterfactual
Engine) adds ``CounterfactualComparisonType`` below -- the 8 comparisons this
layer can actually compute from already-persisted component runs plus
now-known real ``player_gameweek_stats``/``h2h_matches`` outcomes.
``PRODUCTION_VS_BASELINE``/``PRODUCTION_VS_CHALLENGER`` are deliberately NOT
added here -- those require a challenger/shadow-mode concept that doesn't
exist until Layer 5, so adding them now would be speculative surface with
nothing behind it.
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


class CounterfactualComparisonType(str, Enum):
    """One named baseline-vs-alternative comparison ``app.learning.counterfactuals`` can compute.

    See that module's docstring for the exact per-type baseline/alternative
    convention and ``is_value_adding`` directionality, and
    ``app.learning.outcome_ingestion`` for which of these 8 get computed for
    a given ``recommendation_audits`` row (not all 8 apply every gameweek --
    e.g. no H2H comparisons at all when ``h2h_simulation_run_id`` was NULL on
    the audit).
    """

    RECOMMENDED_VS_ACTUAL_TEAM = "recommended_vs_actual_team"
    RECOMMENDED_VS_ACTUAL_CAPTAIN = "recommended_vs_actual_captain"
    TRANSFER_VS_NO_TRANSFER = "transfer_vs_no_transfer"
    TRANSFER_VS_TOP_ALTERNATIVE = "transfer_vs_top_alternative"
    HIT_VS_NO_HIT = "hit_vs_no_hit"
    CHIP_VS_HOLD = "chip_vs_hold"
    OVERALL_VS_H2H_ADJUSTED = "overall_vs_h2h_adjusted"
    CONSERVATIVE_VS_AGGRESSIVE = "conservative_vs_aggressive"
    # PRODUCTION_VS_BASELINE and PRODUCTION_VS_CHALLENGER are Layer 5's concern
    # (challenger/shadow-mode doesn't exist until then) -- deliberately not added yet.
