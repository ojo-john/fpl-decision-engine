"""Modeling domain enums for Milestone 3 Layer 3 (MODEL_TRAINING).

Framework-agnostic (no SQLAlchemy/FastAPI imports here), matching the style
of ``packages/domain/jobs.py``/``packages/domain/ingestion.py``.
"""

from enum import Enum


class ModelFamily(str, Enum):
    """The closed set of candidate model families MODEL_TRAINING evaluates.

    The first four are pure-lookup/formula "baselines" (see
    ``app/modeling/baselines.py``) — no ``.fit()``, no serialized artifact.
    The last three are real scikit-learn tree-based regressors (see
    ``app/modeling/candidates.py``) fit against historical
    ``player_features`` rows and persisted as ``.joblib`` artifacts.
    """

    BASELINE_SEASON_AVERAGE = "baseline_season_average"
    BASELINE_RECENT_AVERAGE = "baseline_recent_average"
    BASELINE_WEIGHTED_RECENT_AVERAGE = "baseline_weighted_recent_average"
    BASELINE_RULES_EP_NEXT = "baseline_rules_ep_next"
    DECISION_TREE = "decision_tree"
    RANDOM_FOREST = "random_forest"
    GRADIENT_BOOSTING = "gradient_boosting"


# The four model families that are pure lookups/formulas with no scikit-learn
# artifact — every ModelVersion row for one of these always has
# artifact_uri=None (see app/modeling/baselines.py and the MODEL_TRAINING
# handler).
BASELINE_MODEL_FAMILIES = frozenset(
    {
        ModelFamily.BASELINE_SEASON_AVERAGE,
        ModelFamily.BASELINE_RECENT_AVERAGE,
        ModelFamily.BASELINE_WEIGHTED_RECENT_AVERAGE,
        ModelFamily.BASELINE_RULES_EP_NEXT,
    }
)
