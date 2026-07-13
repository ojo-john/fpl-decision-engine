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

# Families that can never produce a LIVE prediction, regardless of how well
# they score in walk-forward validation: BASELINE_RULES_EP_NEXT reads
# player_gameweek_stats.xp_estimate for the CURRENT (target) gameweek as its
# historical-backtesting proxy for FPL's own ep_next figure (see
# app/modeling/baselines.py) — but live DATA_INGESTION never populates
# player_gameweek_stats (that needs the /live/ endpoint, deferred to a later
# milestone), so this family always returns None for a live player, 100% of
# the time, not just in edge cases.
#
# Champion selection (app/modeling/champion.py) is deliberately UNAWARE of
# this distinction — picking purely on historical walk-forward accuracy is
# the honest, correct thing for it to do, and this family legitimately can
# be (and often is) the most accurate one historically. Enforcing "but can
# this actually run live" is instead the job of live champion *loading*
# (app/optimization/champion_loader.py), which substitutes the best
# live-capable sibling from the same training run when the flagged champion
# itself falls in this set — otherwise the live optimizer would silently get
# zero benefit from an entire successful training run.
LIVE_INCAPABLE_MODEL_FAMILIES = frozenset({ModelFamily.BASELINE_RULES_EP_NEXT})
