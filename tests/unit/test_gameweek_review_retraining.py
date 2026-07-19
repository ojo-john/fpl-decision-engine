"""Unit tests for app.jobs.handlers.gameweek_review's pure Layer 6 retraining-trigger lookup.

``_find_projection_mae_signal`` is the one piece of Layer 6 decision logic
factored out as a pure, directly-testable function (see that function's own
docstring) -- the actual trigger decision beyond this lookup is just reading
``threshold_breached`` directly, already covered by
``tests/integration/test_gameweek_review_job.py``'s end-to-end scenarios.
"""

from app.jobs.handlers.gameweek_review import _find_projection_mae_signal
from app.learning.report import LearningSignalSummary


def _signal(signal_type: str, threshold_breached: bool = False) -> LearningSignalSummary:
    return LearningSignalSummary(
        signal_type=signal_type,
        window_size_gameweeks=6,
        value=1.0,
        sample_count=10,
        threshold_breached=threshold_breached,
    )


def test_find_projection_mae_signal_returns_the_matching_signal_among_several():
    mae_signal = _signal("rolling_projection_mae", threshold_breached=True)
    signals = [
        _signal("rolling_h2h_win_probability_calibration"),
        mae_signal,
        _signal("rolling_chip_recommendation_value"),
        _signal("failure_reason_concentration"),
    ]
    assert _find_projection_mae_signal(signals) is mae_signal


def test_find_projection_mae_signal_returns_none_when_absent():
    signals = [
        _signal("rolling_h2h_win_probability_calibration"),
        _signal("rolling_chip_recommendation_value"),
    ]
    assert _find_projection_mae_signal(signals) is None


def test_find_projection_mae_signal_returns_none_for_empty_list():
    assert _find_projection_mae_signal([]) is None
