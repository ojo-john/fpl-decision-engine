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

Milestone 7 Layer 3 (Recursive Learning Loop: Failure Attribution/Rolling
Learning Signals) adds ``FailureCategory``/``FailureReasonCode``/
``LearningSignalType`` below -- see each enum's own docstring for the exact
detection rule behind every member and why it is genuinely, mechanically
checkable from data this codebase already persists.

Milestone 7 Layer 4 (Recursive Learning Loop: GAMEWEEK_REVIEW orchestration)
adds ``GameweekReviewRunStatus`` below -- unlike
``RecommendationGenerationRunStatus`` (whose ``RUNNING``/``FAILED`` members
are never actually persisted, see that enum's own docstring), GAMEWEEK_REVIEW
genuinely IS a multi-step orchestrator (it sequences Layers 2/3's own
directly-callable functions) and follows the multi-step optimizer jobs'
create-early-RUNNING-row discipline instead -- so all 3 members here are real,
persisted ``learning_reports.run_status`` values.
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


class FailureCategory(str, Enum):
    """The broad bucket a ``FailureReasonCode`` belongs to (Milestone 7 Layer 3).

    See ``FailureReasonCode``'s own docstring for the exact
    reason-code-to-category mapping and why each one is genuinely, mechanically
    checkable from data this codebase actually persists -- this is deliberately
    NOT a speculative taxonomy designed ahead of what's detectable.
    """

    DATA = "data"
    MINUTES_AVAILABILITY = "minutes_availability"
    PERFORMANCE_MODEL = "performance_model"
    OPTIMIZATION = "optimization"
    H2H = "h2h"
    VARIANCE = "variance"


class FailureReasonCode(str, Enum):
    """One specific, mechanically-classifiable cause ``app.learning.attribution`` can assign to a
    material miss on a ``recommendation_outcomes`` row (Milestone 7 Layer 3).

    Every member below is checkable directly from data this codebase already
    persists -- no member exists purely because the master spec names it as an
    example; each has a concrete detection rule (see
    ``app.learning.attribution`` for the exact classification logic and
    materiality thresholds):

    - ``FPL_API_DEGRADED_OR_BLOCKED`` (``DATA``): ``recommendation_audits
      .fpl_api_health_at_generation`` was ``DEGRADED``/``BLOCKED`` at
      generation time -- a direct field read, no derivation needed. Checked
      FIRST, ahead of every other code, so an FPL-side outage isn't
      misattributed to the model (master spec Section 21).
    - ``RECOMMENDATION_NOT_FOLLOWED`` (``OPTIMIZATION``): the
      ``RECOMMENDED_VS_ACTUAL_TEAM``/``RECOMMENDED_VS_ACTUAL_CAPTAIN``
      counterfactual shows the recommendation would have materially BEATEN
      what was actually played (``is_value_adding=True`` past the materiality
      threshold) -- categorized under ``OPTIMIZATION`` (the recommendation
      pipeline's own output vs. what happened), not a new adherence-specific
      category, to keep ``FailureCategory`` from growing an extra bucket for
      one code.
    - ``PROJECTION_OVERESTIMATED`` / ``PROJECTION_UNDERESTIMATED``
      (``PERFORMANCE_MODEL``): the recommended captain's real, raw
      ``player_gameweek_stats`` points diverged materially from
      ``optimization_run_players.projected_points`` for that same player, AND
      the player genuinely featured (non-zero minutes -- see
      ``PLAYER_ROTATED_OR_UNAVAILABLE`` for the zero-minutes case, which takes
      precedence). Scoped to the CAPTAIN only, not the full XI -- the single
      highest-leverage (doubled) pick, and checking all 11+ starters would
      produce noisy per-player rows with no clear consumer yet.
    - ``PLAYER_ROTATED_OR_UNAVAILABLE`` (``MINUTES_AVAILABILITY``): the
      recommended captain's actual ``player_gameweek_stats.minutes`` was 0
      despite a material projected contribution -- a genuine
      rotation/late-injury miss, mechanically distinct from a projection
      accuracy problem (the model can't be faulted for projecting points for
      a player nobody expected to be benched).
    - ``RIVAL_PREDICTION_ERROR`` (``H2H``): ``RivalProfile
      .predicted_captain_probabilities_json``'s own top (highest-probability)
      predicted captain, when that probability itself was confident enough to
      be worth trusting, does not match ``RivalGameweekSnapshot``'s real
      ``captain_element_id`` for that gameweek (read via
      ``RecommendationOutcome.opponent_actual_captain_player_id``, already
      resolved by Layer 2 -- no new lookup needed there).
    - ``CHIP_TIMING_SUBOPTIMAL`` (``OPTIMIZATION``): ``CHIP_VS_HOLD`` came back
      materially negative (``is_value_adding=False`` past the materiality
      threshold) -- the chip was recommended and played, but genuinely didn't
      pay off versus holding.
    - ``TRANSFER_DECISION_SUBOPTIMAL`` (``OPTIMIZATION``): any of
      ``TRANSFER_VS_NO_TRANSFER``/``TRANSFER_VS_TOP_ALTERNATIVE``/
      ``HIT_VS_NO_HIT`` came back materially negative -- the transfer-side
      analog of ``CHIP_TIMING_SUBOPTIMAL``, same detection shape (a real,
      already-computed counterfactual delta), added because it is exactly as
      mechanically checkable, not padding.
    - ``VARIANCE_UNEXPLAINED`` (``VARIANCE``): a material miss WAS detected
      (the team/captain counterfactual showed the system's own pick
      materially underperforming what was actually played) but none of the
      more specific codes above explain it -- the honest "sometimes football
      is just unpredictable" bucket the master spec's Section 24 explicitly
      calls for, rather than forcing a false-precision attribution.
    - ``RECOMMENDATION_WAS_SOUND``: not a failure at all -- recorded whenever
      NO material miss was found by any of the checks above, so "checked, was
      fine" (one row, this code) is distinguishable from "never checked" (zero
      rows). Requires a ``FailureCategory`` value because that column is
      NOT NULL; ``VARIANCE`` is used as the closest "no specific driver"
      placeholder -- this never implies variance risk was actually realized.

    Deliberately NOT included: anything requiring a signal this codebase
    doesn't actually persist yet (e.g. weather, referee bias, fixture
    congestion fatigue) -- padding this enum with uncheckable categories was
    explicitly avoided.
    """

    FPL_API_DEGRADED_OR_BLOCKED = "fpl_api_degraded_or_blocked"
    RECOMMENDATION_NOT_FOLLOWED = "recommendation_not_followed"
    PROJECTION_OVERESTIMATED = "projection_overestimated"
    PROJECTION_UNDERESTIMATED = "projection_underestimated"
    PLAYER_ROTATED_OR_UNAVAILABLE = "player_rotated_or_unavailable"
    RIVAL_PREDICTION_ERROR = "rival_prediction_error"
    CHIP_TIMING_SUBOPTIMAL = "chip_timing_suboptimal"
    TRANSFER_DECISION_SUBOPTIMAL = "transfer_decision_suboptimal"
    VARIANCE_UNEXPLAINED = "variance_unexplained"
    RECOMMENDATION_WAS_SOUND = "recommendation_was_sound"


class LearningSignalType(str, Enum):
    """One rolling-window computation ``app.learning.signals`` can produce (Milestone 7 Layer 3).

    See that module's docstring for the exact window/threshold/sustained-
    breach logic per signal type -- in particular
    ``ROLLING_CHIP_RECOMMENDATION_VALUE`` is explicitly informational only
    (``threshold_breached`` is always ``False`` for it) and must never be
    treated as a modeling-decay/retraining signal the way
    ``ROLLING_PROJECTION_MAE`` is.
    """

    ROLLING_PROJECTION_MAE = "rolling_projection_mae"
    ROLLING_H2H_WIN_PROBABILITY_CALIBRATION = "rolling_h2h_win_probability_calibration"
    ROLLING_CHIP_RECOMMENDATION_VALUE = "rolling_chip_recommendation_value"
    FAILURE_REASON_CONCENTRATION = "failure_reason_concentration"


class GameweekReviewRunStatus(str, Enum):
    """Lifecycle status of a GAMEWEEK_REVIEW job run (Milestone 7 Layer 4).

    See ``app.jobs.handlers.gameweek_review`` for the full orchestration this
    tracks -- mirrors every other multi-step job's own 3-state run-status
    shape (e.g. ``ChipOptimizationRunStatus``).
    """

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
