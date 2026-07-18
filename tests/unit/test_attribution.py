"""Unit tests for app.learning.attribution's pure classification logic (no DB/network).

Every function tested here takes plain, hand-built values -- no ORM objects,
no session. The orchestrator (``classify_failure_attributions``, which needs
a ``Session`` for the captain-projection/rival-prediction lookups) is covered
by ``tests/integration/test_failure_attribution.py`` instead, against a real
database.
"""

from decimal import Decimal

from app.learning.attribution import (
    _classify_adherence,
    _classify_api_health,
    _classify_chip_timing,
    _classify_transfer_decision,
    _is_system_pick_miss,
    classify_captain_projection_accuracy,
    classify_rival_prediction_error,
    classify_variance_unexplained,
)
from packages.domain.ingestion import EndpointHealth
from packages.domain.learning import CounterfactualComparisonType, FailureCategory, FailureReasonCode

# --- _classify_api_health ----------------------------------------------------------------------------


def test_classify_api_health_healthy_returns_none():
    assert _classify_api_health(EndpointHealth.HEALTHY) is None


def test_classify_api_health_none_returns_none():
    assert _classify_api_health(None) is None


def test_classify_api_health_degraded_attaches_with_evidence():
    result = _classify_api_health(EndpointHealth.DEGRADED)
    assert result is not None
    assert result.reason_code == FailureReasonCode.FPL_API_DEGRADED_OR_BLOCKED
    assert result.category == FailureCategory.DATA
    assert result.evidence == {"fpl_api_health_at_generation": "degraded"}


def test_classify_api_health_blocked_attaches_with_evidence():
    result = _classify_api_health(EndpointHealth.BLOCKED)
    assert result is not None
    assert result.reason_code == FailureReasonCode.FPL_API_DEGRADED_OR_BLOCKED
    assert result.evidence == {"fpl_api_health_at_generation": "blocked"}


# --- _classify_adherence / _is_system_pick_miss -----------------------------------------------------


def test_classify_adherence_material_recommendation_beat_actual_attaches_not_followed():
    result = _classify_adherence(
        CounterfactualComparisonType.RECOMMENDED_VS_ACTUAL_TEAM, delta=8.0, is_value_adding=True
    )
    assert result is not None
    assert result.reason_code == FailureReasonCode.RECOMMENDATION_NOT_FOLLOWED
    assert result.category == FailureCategory.OPTIMIZATION
    assert result.severity == Decimal("8.00")
    assert result.evidence == {
        "counterfactual_comparison_type": "recommended_vs_actual_team",
        "delta": 8.0,
    }


def test_classify_adherence_below_threshold_is_not_attached():
    result = _classify_adherence(
        CounterfactualComparisonType.RECOMMENDED_VS_ACTUAL_TEAM, delta=2.0, is_value_adding=True
    )
    assert result is None


def test_classify_adherence_actual_beat_recommendation_is_not_attached_here():
    # Actual beating the recommendation is the SYSTEM's own miss, not an adherence problem --
    # this function deliberately does not classify that direction (see _is_system_pick_miss).
    result = _classify_adherence(
        CounterfactualComparisonType.RECOMMENDED_VS_ACTUAL_CAPTAIN, delta=-9.0, is_value_adding=False
    )
    assert result is None


def test_is_system_pick_miss_material_actual_beat_recommendation_is_true():
    assert _is_system_pick_miss(delta=-9.0, is_value_adding=False) is True


def test_is_system_pick_miss_below_threshold_is_false():
    assert _is_system_pick_miss(delta=-3.0, is_value_adding=False) is False


def test_is_system_pick_miss_recommendation_beat_actual_is_false():
    assert _is_system_pick_miss(delta=9.0, is_value_adding=True) is False


# --- _classify_transfer_decision --------------------------------------------------------------------


def test_classify_transfer_decision_material_negative_attaches():
    result = _classify_transfer_decision(
        CounterfactualComparisonType.HIT_VS_NO_HIT, delta=-6.0, is_value_adding=False
    )
    assert result is not None
    assert result.reason_code == FailureReasonCode.TRANSFER_DECISION_SUBOPTIMAL
    assert result.category == FailureCategory.OPTIMIZATION
    assert result.severity == Decimal("6.00")


def test_classify_transfer_decision_below_threshold_is_not_attached():
    result = _classify_transfer_decision(
        CounterfactualComparisonType.HIT_VS_NO_HIT, delta=-1.0, is_value_adding=False
    )
    assert result is None


def test_classify_transfer_decision_value_adding_is_not_attached():
    result = _classify_transfer_decision(
        CounterfactualComparisonType.TRANSFER_VS_NO_TRANSFER, delta=9.0, is_value_adding=True
    )
    assert result is None


# --- _classify_chip_timing --------------------------------------------------------------------------


def test_classify_chip_timing_material_miss_attaches():
    result = _classify_chip_timing(delta=-8.5, is_value_adding=False)
    assert result is not None
    assert result.reason_code == FailureReasonCode.CHIP_TIMING_SUBOPTIMAL
    assert result.category == FailureCategory.OPTIMIZATION
    assert result.severity == Decimal("8.50")
    assert result.evidence == {"counterfactual_comparison_type": "chip_vs_hold", "delta": -8.5}


def test_classify_chip_timing_below_threshold_is_not_attached():
    result = _classify_chip_timing(delta=-2.0, is_value_adding=False)
    assert result is None


def test_classify_chip_timing_value_adding_is_not_attached():
    result = _classify_chip_timing(delta=8.5, is_value_adding=True)
    assert result is None


# --- classify_captain_projection_accuracy -----------------------------------------------------------


def test_classify_captain_projection_accuracy_zero_minutes_with_material_projection_is_rotated():
    result = classify_captain_projection_accuracy(
        player_id="p1", projected_points=7.0, actual_raw_points=0.0, actual_minutes=0
    )
    assert result is not None
    assert result.reason_code == FailureReasonCode.PLAYER_ROTATED_OR_UNAVAILABLE
    assert result.category == FailureCategory.MINUTES_AVAILABILITY
    assert result.severity == Decimal("7.00")


def test_classify_captain_projection_accuracy_zero_minutes_with_small_projection_is_none():
    # Model barely expected anything from this player anyway -- zero minutes isn't informative.
    result = classify_captain_projection_accuracy(
        player_id="p1", projected_points=1.0, actual_raw_points=0.0, actual_minutes=0
    )
    assert result is None


def test_classify_captain_projection_accuracy_played_but_overestimated():
    result = classify_captain_projection_accuracy(
        player_id="p1", projected_points=8.0, actual_raw_points=1.0, actual_minutes=90
    )
    assert result is not None
    assert result.reason_code == FailureReasonCode.PROJECTION_OVERESTIMATED
    assert result.category == FailureCategory.PERFORMANCE_MODEL
    assert result.severity == Decimal("7.00")


def test_classify_captain_projection_accuracy_played_but_underestimated():
    result = classify_captain_projection_accuracy(
        player_id="p1", projected_points=2.0, actual_raw_points=14.0, actual_minutes=90
    )
    assert result is not None
    assert result.reason_code == FailureReasonCode.PROJECTION_UNDERESTIMATED
    assert result.category == FailureCategory.PERFORMANCE_MODEL
    assert result.severity == Decimal("12.00")


def test_classify_captain_projection_accuracy_within_tolerance_is_none():
    result = classify_captain_projection_accuracy(
        player_id="p1", projected_points=6.0, actual_raw_points=8.0, actual_minutes=90
    )
    assert result is None


# --- classify_rival_prediction_error -----------------------------------------------------------------


def test_classify_rival_prediction_error_confident_and_wrong_attaches():
    result = classify_rival_prediction_error(
        predicted_captain_probabilities={"111": 0.6, "222": 0.4}, actual_captain_element_id=222
    )
    assert result is not None
    assert result.reason_code == FailureReasonCode.RIVAL_PREDICTION_ERROR
    assert result.category == FailureCategory.H2H
    assert result.evidence["predicted_captain_element_id"] == 111
    assert result.evidence["actual_captain_element_id"] == 222


def test_classify_rival_prediction_error_confident_and_correct_is_none():
    result = classify_rival_prediction_error(
        predicted_captain_probabilities={"111": 0.6, "222": 0.4}, actual_captain_element_id=111
    )
    assert result is None


def test_classify_rival_prediction_error_low_confidence_is_none():
    # Near-uniform distribution -- an honest "didn't know", not a prediction error.
    result = classify_rival_prediction_error(
        predicted_captain_probabilities={"111": 0.34, "222": 0.33, "333": 0.33},
        actual_captain_element_id=222,
    )
    assert result is None


def test_classify_rival_prediction_error_no_prediction_data_is_none():
    assert classify_rival_prediction_error(None, actual_captain_element_id=111) is None


def test_classify_rival_prediction_error_no_actual_captain_is_none():
    assert classify_rival_prediction_error({"111": 0.6}, actual_captain_element_id=None) is None


# --- classify_variance_unexplained --------------------------------------------------------------------


def test_classify_variance_unexplained_triggered_attaches():
    result = classify_variance_unexplained(True, {"note": "test"})
    assert result is not None
    assert result.reason_code == FailureReasonCode.VARIANCE_UNEXPLAINED
    assert result.category == FailureCategory.VARIANCE
    assert result.evidence == {"note": "test"}


def test_classify_variance_unexplained_not_triggered_is_none():
    assert classify_variance_unexplained(False, {}) is None
