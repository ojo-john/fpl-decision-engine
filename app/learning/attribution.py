"""Failure attribution (Milestone 7 Layer 3: Recursive Learning Loop -- Failure Attribution/Rolling
Learning Signals).

Examines one already-computed ``recommendation_outcomes`` row's own
``counterfactual_results`` children (Milestone 7 Layer 2's output) plus the
original ``recommendation_audits`` row's generation-time context, and assigns
zero or more ``FailureReasonCode``s with concrete, materiality-scored
evidence. See ``packages.domain.learning.FailureReasonCode`` for the full,
documented reason-code-to-category mapping and exactly which persisted field
each code's detection rule reads -- nothing here is speculative or
un-checkable from data this codebase actually has.

Module layout, mirroring ``app.learning.counterfactuals``/
``app.learning.outcome_ingestion``'s own "pure computation vs. DB-session
orchestrator" split as closely as this problem allows: every ``_classify_*``
function below is PURE (plain values in, an optional ``FailureAttributionData``
out) and directly unit-testable without a DB. Only
``classify_failure_attributions`` itself takes a ``Session`` -- it needs one
for exactly two lookups a plain ``CounterfactualResult`` list can't supply:
(1) the recommended captain's own ``optimization_run_players.projected_points``
+ real ``player_gameweek_stats`` (points, minutes) for the projection-accuracy/
rotation checks, and (2) the rival's ``RivalProfile.predicted_captain_probabilities_json``
for the H2H rival-prediction check. Both lookups are resolved once, up front,
and handed to their respective pure classifier.

Ordering/precedence: ``FPL_API_DEGRADED_OR_BLOCKED`` is checked and appended
FIRST when applicable (master spec Section 21's own "so a bad call caused by
an FPL-side outage isn't misattributed to the model" reasoning) -- but a
degraded-API gameweek's OTHER counterfactual misses are still checked and
recorded too, never skipped, so the API context is additional evidence, not a
silent override.

Materiality thresholds (named constants, see each one's own comment for the
reasoning) exist so small, real-world-irrelevant deltas never generate an
attribution row at all -- a below-threshold ``RECOMMENDED_VS_ACTUAL_TEAM``/
``RECOMMENDED_VS_ACTUAL_CAPTAIN`` delta in the "recommendation was better"
direction is exactly as uninteresting as one in the other direction, and both
are silently skipped rather than over-attributed as noise.

If NO material miss is found by any check, exactly one
``RECOMMENDATION_WAS_SOUND`` row is returned (never zero rows) -- see that
enum member's own docstring for why "checked, was fine" must stay
distinguishable from "never checked".
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.learning.actuals import actual_minutes_played, sum_actual_points
from packages.database.models.counterfactual_result import CounterfactualResult
from packages.database.models.failure_attribution import FailureAttribution
from packages.database.models.h2h_simulation_run import H2HSimulationRun
from packages.database.models.optimization_run_player import OptimizationRunPlayer
from packages.database.models.recommendation_audit import RecommendationAudit
from packages.database.models.recommendation_outcome import RecommendationOutcome
from packages.database.models.rival_profile import RivalProfile
from packages.domain.ingestion import EndpointHealth
from packages.domain.learning import CounterfactualComparisonType, FailureCategory, FailureReasonCode

__all__ = [
    "FailureAttributionData",
    "classify_failure_attributions",
    "persist_failure_attributions",
    "attribute_failures_for_outcome",
]

# --- Named materiality thresholds -----------------------------------------------------------------
# General team/transfer/chip/H2H counterfactual materiality bar. Real FPL gameweek totals routinely
# swing +/-5 points purely from bonus-point allocation/haul variance -- below this, calling a
# counterfactual delta a "failure" would just be attributing normal noise, not a real miss.
_MATERIAL_DELTA_THRESHOLD_POINTS = 5.0

# Single-player (captain) raw projection-error bar -- a smaller absolute swing than the team-level
# bar above, since it concerns one player rather than a full XI, but still well above single
# bonus-point noise (a genuine haul vs. a blank routinely differs by 4+ raw points).
_MATERIAL_PROJECTION_ERROR_POINTS = 4.0

# A rival-captain prediction is only "wrong" in a meaningful sense if the model's own top candidate
# carried real plurality confidence -- a near-uniform distribution over several plausible captains
# reflects an honest "didn't know", not a prediction error.
_RIVAL_PREDICTION_CONFIDENCE_THRESHOLD = 0.40

# Category assigned to RECOMMENDATION_WAS_SOUND purely because `category` is NOT NULL on the
# FailureAttribution table -- see that enum member's own docstring for why VARIANCE (the closest
# "no specific driver" bucket) was chosen, and why this never implies variance risk was realized.
_RECOMMENDATION_WAS_SOUND_CATEGORY = FailureCategory.VARIANCE


@dataclass(frozen=True)
class FailureAttributionData:
    """One computed reason-code classification, ready for ``FailureAttribution`` persistence."""

    reason_code: FailureReasonCode
    category: FailureCategory
    severity: Decimal | None
    evidence: dict


# --- Pure classifiers -------------------------------------------------------------------------------


def _classify_api_health(fpl_api_health: EndpointHealth | None) -> FailureAttributionData | None:
    """FPL_API_DEGRADED_OR_BLOCKED: a direct read of ``recommendation_audits
    .fpl_api_health_at_generation`` -- no derivation needed."""
    if fpl_api_health not in (EndpointHealth.DEGRADED, EndpointHealth.BLOCKED):
        return None
    return FailureAttributionData(
        reason_code=FailureReasonCode.FPL_API_DEGRADED_OR_BLOCKED,
        category=FailureCategory.DATA,
        severity=None,
        evidence={"fpl_api_health_at_generation": fpl_api_health.value},
    )


def _classify_adherence(
    comparison_type: CounterfactualComparisonType,
    delta: float,
    is_value_adding: bool,
    threshold: float = _MATERIAL_DELTA_THRESHOLD_POINTS,
) -> FailureAttributionData | None:
    """RECOMMENDATION_NOT_FOLLOWED: the recommendation would have materially BEATEN what was
    actually played (``is_value_adding=True``, i.e. baseline=actual, alternative=recommended, and
    the recommendation -- the alternative -- scored more). The opposite direction (actual beat the
    recommendation) is the system's OWN pick underperforming, which this function deliberately does
    NOT classify -- see ``_is_system_pick_miss``/the projection-accuracy checks below for that case.
    """
    if is_value_adding and abs(delta) >= threshold:
        return FailureAttributionData(
            reason_code=FailureReasonCode.RECOMMENDATION_NOT_FOLLOWED,
            category=FailureCategory.OPTIMIZATION,
            severity=Decimal(str(round(abs(delta), 2))),
            evidence={"counterfactual_comparison_type": comparison_type.value, "delta": round(delta, 2)},
        )
    return None


def _is_system_pick_miss(
    delta: float, is_value_adding: bool, threshold: float = _MATERIAL_DELTA_THRESHOLD_POINTS
) -> bool:
    """``True`` when a RECOMMENDED_VS_ACTUAL_TEAM/CAPTAIN comparison shows the system's OWN pick
    (the "alternative") materially underperforming what was actually played (the "baseline") --
    i.e. NOT an adherence problem, a genuine system miss that ``VARIANCE_UNEXPLAINED`` or a more
    specific projection-accuracy code should explain.
    """
    return (not is_value_adding) and abs(delta) >= threshold


def _classify_transfer_decision(
    comparison_type: CounterfactualComparisonType,
    delta: float,
    is_value_adding: bool,
    threshold: float = _MATERIAL_DELTA_THRESHOLD_POINTS,
) -> FailureAttributionData | None:
    """TRANSFER_DECISION_SUBOPTIMAL: any of TRANSFER_VS_NO_TRANSFER/TRANSFER_VS_TOP_ALTERNATIVE/
    HIT_VS_NO_HIT came back materially negative -- the transfer-side analog of
    ``_classify_chip_timing``, same detection shape.
    """
    if (not is_value_adding) and abs(delta) >= threshold:
        return FailureAttributionData(
            reason_code=FailureReasonCode.TRANSFER_DECISION_SUBOPTIMAL,
            category=FailureCategory.OPTIMIZATION,
            severity=Decimal(str(round(abs(delta), 2))),
            evidence={"counterfactual_comparison_type": comparison_type.value, "delta": round(delta, 2)},
        )
    return None


def _classify_chip_timing(
    delta: float, is_value_adding: bool, threshold: float = _MATERIAL_DELTA_THRESHOLD_POINTS
) -> FailureAttributionData | None:
    """CHIP_TIMING_SUBOPTIMAL: CHIP_VS_HOLD came back materially negative -- the chip was
    recommended and played, but genuinely didn't pay off versus holding.
    """
    if (not is_value_adding) and abs(delta) >= threshold:
        return FailureAttributionData(
            reason_code=FailureReasonCode.CHIP_TIMING_SUBOPTIMAL,
            category=FailureCategory.OPTIMIZATION,
            severity=Decimal(str(round(abs(delta), 2))),
            evidence={
                "counterfactual_comparison_type": CounterfactualComparisonType.CHIP_VS_HOLD.value,
                "delta": round(delta, 2),
            },
        )
    return None


def classify_captain_projection_accuracy(
    player_id: str,
    projected_points: float,
    actual_raw_points: float,
    actual_minutes: int,
    error_threshold: float = _MATERIAL_PROJECTION_ERROR_POINTS,
) -> FailureAttributionData | None:
    """PLAYER_ROTATED_OR_UNAVAILABLE / PROJECTION_OVERESTIMATED / PROJECTION_UNDERESTIMATED for the
    recommended captain specifically -- see ``FailureReasonCode``'s own docstring for why this is
    scoped to just the captain (highest-leverage doubled pick) rather than the full XI.

    Zero real minutes takes precedence over a raw points-based projection read: a player who never
    featured is a rotation/availability miss, not a genuine projection-accuracy miss (the model
    cannot be faulted for a late benching/injury it had no way to know about at generation time).
    Only flagged when the model projected a material contribution in the first place (a low
    projection paired with zero minutes is simply the projection being roughly right that this
    player wasn't a key contributor -- not itself informative).
    """
    if actual_minutes == 0:
        if projected_points >= error_threshold:
            return FailureAttributionData(
                reason_code=FailureReasonCode.PLAYER_ROTATED_OR_UNAVAILABLE,
                category=FailureCategory.MINUTES_AVAILABILITY,
                severity=Decimal(str(round(projected_points, 2))),
                evidence={
                    "player_id": player_id,
                    "projected_points": round(projected_points, 2),
                    "actual_minutes": actual_minutes,
                },
            )
        return None

    error = actual_raw_points - projected_points
    if error <= -error_threshold:
        return FailureAttributionData(
            reason_code=FailureReasonCode.PROJECTION_OVERESTIMATED,
            category=FailureCategory.PERFORMANCE_MODEL,
            severity=Decimal(str(round(abs(error), 2))),
            evidence={
                "player_id": player_id,
                "projected_points": round(projected_points, 2),
                "actual_raw_points": round(actual_raw_points, 2),
            },
        )
    if error >= error_threshold:
        return FailureAttributionData(
            reason_code=FailureReasonCode.PROJECTION_UNDERESTIMATED,
            category=FailureCategory.PERFORMANCE_MODEL,
            severity=Decimal(str(round(abs(error), 2))),
            evidence={
                "player_id": player_id,
                "projected_points": round(projected_points, 2),
                "actual_raw_points": round(actual_raw_points, 2),
            },
        )
    return None


def classify_rival_prediction_error(
    predicted_captain_probabilities: dict[str, float] | None,
    actual_captain_element_id: int | None,
    confidence_threshold: float = _RIVAL_PREDICTION_CONFIDENCE_THRESHOLD,
) -> FailureAttributionData | None:
    """RIVAL_PREDICTION_ERROR: the rival profile's own top (highest-probability) predicted captain,
    when that prediction itself carried real confidence, doesn't match the rival's real captain for
    that gameweek.
    """
    if not predicted_captain_probabilities or actual_captain_element_id is None:
        return None
    top_element_id_raw, top_probability = max(
        predicted_captain_probabilities.items(), key=lambda item: item[1]
    )
    if top_probability < confidence_threshold:
        return None
    try:
        top_element_id = int(top_element_id_raw)
    except (TypeError, ValueError):
        return None
    if top_element_id == actual_captain_element_id:
        return None
    return FailureAttributionData(
        reason_code=FailureReasonCode.RIVAL_PREDICTION_ERROR,
        category=FailureCategory.H2H,
        severity=Decimal(str(round(top_probability, 4))),
        evidence={
            "predicted_captain_element_id": top_element_id,
            "predicted_probability": round(top_probability, 4),
            "actual_captain_element_id": actual_captain_element_id,
        },
    )


def classify_variance_unexplained(triggered: bool, evidence: dict) -> FailureAttributionData | None:
    """VARIANCE_UNEXPLAINED: a material system-pick miss was detected, but no more specific code
    explains it -- the honest "sometimes football is just unpredictable" bucket (master spec
    Section 24).
    """
    if not triggered:
        return None
    return FailureAttributionData(
        reason_code=FailureReasonCode.VARIANCE_UNEXPLAINED,
        category=FailureCategory.VARIANCE,
        severity=None,
        evidence=evidence,
    )


# --- Orchestration (needs a Session for exactly two lookups) ---------------------------------------


def _resolve_recommended_captain_projection(
    session: Session, audit: RecommendationAudit
) -> OptimizationRunPlayer | None:
    if audit.optimization_run_id is None:
        return None
    return session.execute(
        select(OptimizationRunPlayer).where(
            OptimizationRunPlayer.optimization_run_id == audit.optimization_run_id,
            OptimizationRunPlayer.is_captain.is_(True),
        )
    ).scalar_one_or_none()


def _resolve_rival_predicted_captain_probabilities(
    session: Session, audit: RecommendationAudit
) -> dict[str, float] | None:
    if audit.h2h_simulation_run_id is None:
        return None
    h2h_run = session.get(H2HSimulationRun, audit.h2h_simulation_run_id)
    if h2h_run is None:
        return None
    rival_profile = session.get(RivalProfile, h2h_run.rival_profile_id)
    if rival_profile is None:
        return None
    return rival_profile.predicted_captain_probabilities_json


def classify_failure_attributions(
    session: Session,
    recommendation_outcome: RecommendationOutcome,
    counterfactual_results: list[CounterfactualResult],
    recommendation_audit: RecommendationAudit,
) -> list[FailureAttributionData]:
    """Examine one finished ``recommendation_outcome``'s counterfactual results plus its original
    audit's generation-time context, and assign zero-or-more ``FailureReasonCode``s.

    See module docstring for ordering/precedence and the pure-vs-orchestrator split.
    """
    attributions: list[FailureAttributionData] = []

    api_attribution = _classify_api_health(recommendation_audit.fpl_api_health_at_generation)
    if api_attribution is not None:
        attributions.append(api_attribution)

    results_by_type = {row.comparison_type: row for row in counterfactual_results}

    system_pick_miss_triggered = False
    system_pick_miss_evidence: dict = {}

    for comparison_type in (
        CounterfactualComparisonType.RECOMMENDED_VS_ACTUAL_TEAM,
        CounterfactualComparisonType.RECOMMENDED_VS_ACTUAL_CAPTAIN,
    ):
        result = results_by_type.get(comparison_type)
        if result is None:
            continue
        delta = float(result.delta)
        adherence = _classify_adherence(comparison_type, delta, result.is_value_adding)
        if adherence is not None:
            attributions.append(adherence)
        elif _is_system_pick_miss(delta, result.is_value_adding):
            system_pick_miss_triggered = True
            system_pick_miss_evidence = {
                "counterfactual_comparison_type": comparison_type.value,
                "delta": round(delta, 2),
            }

    for comparison_type in (
        CounterfactualComparisonType.TRANSFER_VS_NO_TRANSFER,
        CounterfactualComparisonType.TRANSFER_VS_TOP_ALTERNATIVE,
        CounterfactualComparisonType.HIT_VS_NO_HIT,
    ):
        result = results_by_type.get(comparison_type)
        if result is None:
            continue
        transfer_attribution = _classify_transfer_decision(
            comparison_type, float(result.delta), result.is_value_adding
        )
        if transfer_attribution is not None:
            attributions.append(transfer_attribution)

    chip_result = results_by_type.get(CounterfactualComparisonType.CHIP_VS_HOLD)
    if chip_result is not None:
        chip_attribution = _classify_chip_timing(float(chip_result.delta), chip_result.is_value_adding)
        if chip_attribution is not None:
            attributions.append(chip_attribution)

    captain_projection = _resolve_recommended_captain_projection(session, recommendation_audit)
    projection_attribution: FailureAttributionData | None = None
    if captain_projection is not None:
        actual_raw_points = sum_actual_points(
            session, [captain_projection.player_id], recommendation_audit.target_gameweek_id
        )
        minutes = actual_minutes_played(
            session, captain_projection.player_id, recommendation_audit.target_gameweek_id
        )
        projection_attribution = classify_captain_projection_accuracy(
            player_id=str(captain_projection.player_id),
            projected_points=float(captain_projection.projected_points),
            actual_raw_points=actual_raw_points,
            actual_minutes=minutes,
        )
    if projection_attribution is not None:
        attributions.append(projection_attribution)
    elif system_pick_miss_triggered:
        # A material team/captain miss was detected, but the captain-specific
        # projection/rotation check above didn't explain it (e.g. the miss was
        # driven by a non-captain starter, or there was no resolvable
        # optimization_run_id at all) -- the honest catch-all bucket.
        variance_attribution = classify_variance_unexplained(True, system_pick_miss_evidence)
        if variance_attribution is not None:
            attributions.append(variance_attribution)

    predicted_captain_probabilities = _resolve_rival_predicted_captain_probabilities(
        session, recommendation_audit
    )
    rival_attribution = classify_rival_prediction_error(
        predicted_captain_probabilities, recommendation_outcome.opponent_actual_captain_player_id
    )
    if rival_attribution is not None:
        attributions.append(rival_attribution)

    if not attributions:
        attributions.append(
            FailureAttributionData(
                reason_code=FailureReasonCode.RECOMMENDATION_WAS_SOUND,
                category=_RECOMMENDATION_WAS_SOUND_CATEGORY,
                severity=None,
                evidence={"note": "no material misses detected across any checked signal"},
            )
        )

    return attributions


def persist_failure_attributions(
    session: Session, recommendation_outcome_id: uuid.UUID, attributions: list[FailureAttributionData]
) -> list[FailureAttribution]:
    """Persist ``attributions`` for one ``recommendation_outcome_id``, wiping any prior rows first.

    Mirrors ``app.learning.outcome_ingestion``'s own delete-then-reinsert
    convention for a rerun of the same parent (a re-classification is exactly
    the case where newly-corrected ``player_gameweek_stats``/counterfactual
    data should legitimately replace a previous attribution set, not
    accumulate alongside it) -- individual rows are still never UPDATEd in
    place, only deleted and freshly inserted, matching this table's own
    insert-only-child docstring.
    """
    session.execute(
        delete(FailureAttribution).where(
            FailureAttribution.recommendation_outcome_id == recommendation_outcome_id
        )
    )
    rows = [
        FailureAttribution(
            recommendation_outcome_id=recommendation_outcome_id,
            reason_code=attribution.reason_code,
            category=attribution.category,
            severity=attribution.severity,
            evidence_json=attribution.evidence,
        )
        for attribution in attributions
    ]
    session.add_all(rows)
    session.commit()
    for row in rows:
        session.refresh(row)
    return rows


def attribute_failures_for_outcome(
    session: Session, recommendation_outcome_id: uuid.UUID
) -> list[FailureAttribution]:
    """Convenience entry point: load a ``recommendation_outcome`` and its own children, classify, and
    persist -- the single callable a future ``GAMEWEEK_REVIEW`` job (Milestone 7 Layer 4, not built
    yet) will use as its own building block, mirroring
    ``app.learning.outcome_ingestion.ingest_outcome_for_audit``'s own "directly callable today" role
    for Layer 2.
    """
    outcome = session.get(RecommendationOutcome, recommendation_outcome_id)
    if outcome is None:
        raise ValueError(f"recommendation_outcomes row {recommendation_outcome_id} not found.")
    audit = session.get(RecommendationAudit, outcome.recommendation_audit_id)
    if audit is None:
        raise ValueError(
            f"recommendation_audits row {outcome.recommendation_audit_id} not found for "
            f"recommendation_outcome {recommendation_outcome_id}."
        )
    counterfactual_results = (
        session.execute(
            select(CounterfactualResult).where(
                CounterfactualResult.recommendation_outcome_id == recommendation_outcome_id
            )
        )
        .scalars()
        .all()
    )
    attributions = classify_failure_attributions(session, outcome, list(counterfactual_results), audit)
    return persist_failure_attributions(session, recommendation_outcome_id, attributions)
