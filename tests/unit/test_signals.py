"""Unit tests for app.learning.signals's pure sustained-window/concentration logic (no DB/network).

The ``compute_rolling_*``/``compute_failure_reason_concentration`` functions
themselves are DB-session-taking queries, covered by
``tests/integration/test_learning_signals.py`` against a real database --
this module only covers the two genuinely pure, factored-out decisions:
``_is_sustained_breach`` (is a degradation pattern sustained, not a blip?) and
``_reason_code_concentration`` (which reason code repeats most, and how
often?).
"""

from app.learning.signals import _is_sustained_breach, _reason_code_concentration
from packages.domain.learning import FailureReasonCode

# --- _is_sustained_breach ----------------------------------------------------------------------------


def test_is_sustained_breach_single_gameweek_blip_is_not_breached():
    # Only the very last entry is degraded -- a blip, not a sustained pattern.
    per_gameweek = [1.0, 1.0, 1.0, 5.0]
    assert _is_sustained_breach(per_gameweek, threshold=2.0, min_consecutive=3) is False


def test_is_sustained_breach_genuinely_sustained_degradation_is_breached():
    # The last 3 (of 4) gameweeks are all degraded -- a real, current pattern.
    per_gameweek = [1.0, 5.0, 5.0, 5.0]
    assert _is_sustained_breach(per_gameweek, threshold=2.0, min_consecutive=3) is True


def test_is_sustained_breach_stale_degradation_that_has_resolved_is_not_breached():
    # Degraded a few gameweeks ago, but the most recent gameweeks are healthy again.
    per_gameweek = [5.0, 5.0, 5.0, 1.0, 1.0]
    assert _is_sustained_breach(per_gameweek, threshold=2.0, min_consecutive=3) is False


def test_is_sustained_breach_insufficient_history_is_not_breached():
    per_gameweek = [5.0, 5.0]
    assert _is_sustained_breach(per_gameweek, threshold=2.0, min_consecutive=3) is False


def test_is_sustained_breach_empty_history_is_not_breached():
    assert _is_sustained_breach([], threshold=2.0, min_consecutive=3) is False


def test_is_sustained_breach_all_healthy_is_not_breached():
    per_gameweek = [1.0, 1.0, 1.0, 1.0]
    assert _is_sustained_breach(per_gameweek, threshold=2.0, min_consecutive=3) is False


def test_is_sustained_breach_exactly_at_threshold_counts_as_breached():
    per_gameweek = [2.0, 2.0, 2.0]
    assert _is_sustained_breach(per_gameweek, threshold=2.0, min_consecutive=3) is True


# --- _reason_code_concentration ----------------------------------------------------------------------


def test_reason_code_concentration_repeated_pattern_is_detected():
    batches = [
        {FailureReasonCode.CHIP_TIMING_SUBOPTIMAL},
        {FailureReasonCode.CHIP_TIMING_SUBOPTIMAL, FailureReasonCode.VARIANCE_UNEXPLAINED},
        {FailureReasonCode.CHIP_TIMING_SUBOPTIMAL},
        {FailureReasonCode.VARIANCE_UNEXPLAINED},
    ]
    top_code, count = _reason_code_concentration(batches)
    assert top_code == FailureReasonCode.CHIP_TIMING_SUBOPTIMAL
    assert count == 3


def test_reason_code_concentration_varied_pattern_has_low_max_count():
    batches = [
        {FailureReasonCode.CHIP_TIMING_SUBOPTIMAL},
        {FailureReasonCode.PROJECTION_OVERESTIMATED},
        {FailureReasonCode.RIVAL_PREDICTION_ERROR},
        {FailureReasonCode.TRANSFER_DECISION_SUBOPTIMAL},
    ]
    top_code, count = _reason_code_concentration(batches)
    assert count == 1
    assert top_code is not None


def test_reason_code_concentration_excludes_recommendation_was_sound():
    batches = [
        {FailureReasonCode.RECOMMENDATION_WAS_SOUND},
        {FailureReasonCode.RECOMMENDATION_WAS_SOUND},
        {FailureReasonCode.RECOMMENDATION_WAS_SOUND},
    ]
    top_code, count = _reason_code_concentration(batches)
    assert top_code is None
    assert count == 0


def test_reason_code_concentration_empty_batches_returns_none():
    top_code, count = _reason_code_concentration([])
    assert top_code is None
    assert count == 0
