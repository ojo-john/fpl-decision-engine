"""Unit tests for app.modeling.metrics: every metric against small, hand-computed arrays with KNOWN answers.

No database, no network.
"""

import math

import pytest

from app.modeling.metrics import (
    binary_accuracy,
    binary_auc,
    binary_f1,
    binary_log_loss,
    calibration_buckets,
    mean_absolute_error,
    mean_bias,
    root_mean_squared_error,
    spearman_rank_correlation,
    top_k_capture_rate,
)

# --- mean_absolute_error / root_mean_squared_error ----------------------


def test_mean_absolute_error_hand_computed():
    # |1-1| + |2-2| + |3-4| = 0 + 0 + 1 -> mean = 1/3
    assert mean_absolute_error([1, 2, 3], [1, 2, 4]) == pytest.approx(1 / 3)


def test_root_mean_squared_error_hand_computed():
    # squared errors: 0, 0, 1 -> mean = 1/3 -> sqrt(1/3)
    assert root_mean_squared_error([1, 2, 3], [1, 2, 4]) == pytest.approx(math.sqrt(1 / 3))


# --- mean_bias: sign correctness -----------------------------------------


def test_mean_bias_positive_for_systematic_overprediction():
    # every prediction is 1 higher than truth -> mean bias = +1.0
    assert mean_bias([1, 2, 3], [2, 3, 4]) == pytest.approx(1.0)


def test_mean_bias_negative_for_systematic_underprediction():
    # every prediction is 1 lower than truth -> mean bias = -1.0
    assert mean_bias([2, 3, 4], [1, 2, 3]) == pytest.approx(-1.0)


def test_mean_bias_zero_for_unbiased_predictions():
    assert mean_bias([1, 2, 3], [1, 2, 3]) == pytest.approx(0.0)


# --- spearman_rank_correlation -------------------------------------------


def test_spearman_rank_correlation_perfect_positive():
    assert spearman_rank_correlation([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)


def test_spearman_rank_correlation_perfect_negative():
    assert spearman_rank_correlation([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)


def test_spearman_rank_correlation_none_when_true_has_fewer_than_2_distinct_values():
    assert spearman_rank_correlation([5, 5, 5], [1, 2, 3]) is None


def test_spearman_rank_correlation_none_when_pred_has_fewer_than_2_distinct_values():
    assert spearman_rank_correlation([1, 2, 3], [5, 5, 5]) is None


# --- top_k_capture_rate ---------------------------------------------------


def test_top_k_capture_rate_hand_constructed_ranking():
    # true top-3 by value (indices 2,3,4 -> values 30,40,50)
    y_true = [10, 20, 30, 40, 50]
    # predicted top-3 by value (indices 1,3,4 -> values 100,90,80)
    y_pred = [5, 100, 1, 90, 80]
    # overlap of {2,3,4} and {1,3,4} = {3,4} -> 2/3
    assert top_k_capture_rate(y_true, y_pred, k=3) == pytest.approx(2 / 3)


def test_top_k_capture_rate_perfect_when_rankings_match():
    y_true = [10, 20, 30, 40, 50]
    y_pred = [1, 2, 3, 4, 5]
    assert top_k_capture_rate(y_true, y_pred, k=3) == pytest.approx(1.0)


def test_top_k_capture_rate_none_when_fewer_than_k_observations():
    assert top_k_capture_rate([1, 2], [1, 2], k=20) is None


# --- calibration_buckets --------------------------------------------------


def test_calibration_buckets_hand_verified_boundaries_and_means():
    y_pred = [1, 2, 3, 4, 5, 6]
    y_true = [10, 20, 30, 40, 50, 60]

    buckets = calibration_buckets(y_true, y_pred, n_buckets=3)

    assert set(buckets.keys()) == {0, 1, 2}

    assert buckets[0]["n"] == 2
    assert buckets[0]["mean_predicted"] == pytest.approx(1.5)
    assert buckets[0]["mean_actual"] == pytest.approx(15.0)
    assert buckets[0]["pred_min"] == pytest.approx(1.0)
    assert buckets[0]["pred_max"] == pytest.approx(2.0)

    assert buckets[1]["mean_predicted"] == pytest.approx(3.5)
    assert buckets[1]["mean_actual"] == pytest.approx(35.0)

    assert buckets[2]["mean_predicted"] == pytest.approx(5.5)
    assert buckets[2]["mean_actual"] == pytest.approx(55.0)


def test_calibration_buckets_bucket_assignment_by_sorted_predicted_value_not_input_order():
    # Same pairing as the test above but given in reverse (descending
    # predicted-value) order -- bucketing must sort by predicted value
    # first, so the result should be identical regardless of input order.
    y_pred = [6, 5, 4, 3, 2, 1]
    y_true = [60, 50, 40, 30, 20, 10]

    buckets = calibration_buckets(y_true, y_pred, n_buckets=3)

    assert buckets[0]["mean_predicted"] == pytest.approx(1.5)
    assert buckets[0]["mean_actual"] == pytest.approx(15.0)
    assert buckets[2]["mean_predicted"] == pytest.approx(5.5)
    assert buckets[2]["mean_actual"] == pytest.approx(55.0)


# --- binary_auc / binary_accuracy / binary_f1 / binary_log_loss (app.modeling.start_probability) -


def test_binary_auc_perfect_separation_is_one():
    # every negative scores lower than every positive -> AUC = 1.0
    assert binary_auc([0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9]) == pytest.approx(1.0)


def test_binary_auc_undefined_with_a_single_class_returns_none():
    assert binary_auc([1, 1, 1], [0.9, 0.8, 0.7]) is None


def test_binary_accuracy_hand_computed():
    # 3 of 4 predicted labels match truth -> 0.75
    assert binary_accuracy([1, 0, 1, 0], [1, 0, 1, 1]) == pytest.approx(0.75)


def test_binary_f1_hand_computed():
    # y_true=[1,1,0,0], y_pred=[1,0,1,0] -> TP=1, FP=1, FN=1 -> precision=0.5, recall=0.5, f1=0.5
    assert binary_f1([1, 1, 0, 0], [1, 0, 1, 0]) == pytest.approx(0.5)


def test_binary_f1_no_positive_predictions_is_zero_not_a_warning_or_nan():
    assert binary_f1([1, 1, 0, 0], [0, 0, 0, 0]) == pytest.approx(0.0)


def test_binary_log_loss_confident_and_correct_is_near_zero():
    assert binary_log_loss([0, 1], [0.001, 0.999]) == pytest.approx(0.0, abs=1e-2)


def test_binary_log_loss_undefined_with_a_single_class_returns_none():
    assert binary_log_loss([1, 1, 1], [0.9, 0.8, 0.7]) is None
