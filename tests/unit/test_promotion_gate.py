"""Unit tests for app.modeling.promotion's pure gate-decision logic (``_evaluate_gate``).

No database, no network -- synthetic per-gameweek MAE pairs with a known
correct answer. Mirrors ``tests/unit/test_champion_selection.py``'s own
"synthetic multi-candidate metric sets" style.
"""

from app.modeling.promotion import _evaluate_gate
from packages.domain.learning import PromotionVerdict


def test_insufficient_window_retains_inconclusive_regardless_of_how_good_numbers_look():
    """Fewer than min_evaluation_window observations -- even a huge apparent improvement -- must never
    force a verdict."""
    result = _evaluate_gate(
        challenger_maes=[1.0, 1.0],
        champion_maes=[5.0, 5.0],
        min_evaluation_window=6,
        mae_improvement_margin=0.05,
    )

    assert result.verdict == PromotionVerdict.RETAINED_INCONCLUSIVE
    assert result.n_observations == 2
    assert result.gate_checks["minimum_sample_size"] is False
    assert result.gate_checks["beats_champion_by_margin"] is False
    # Gates not implemented in this codebase are always reported as passing/not-applicable.
    assert result.gate_checks["per_position_segmentation"] is True
    assert result.gate_checks["historical_backtest_replay"] is True


def test_challenger_clearly_beats_margin_over_full_window_promotes():
    # champion mean MAE = 2.0; margin=5% -> required <= 1.9; challenger mean MAE = 1.0 clears it easily.
    result = _evaluate_gate(
        challenger_maes=[1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
        champion_maes=[2.0, 2.0, 2.0, 2.0, 2.0, 2.0],
        min_evaluation_window=6,
        mae_improvement_margin=0.05,
    )

    assert result.verdict == PromotionVerdict.PROMOTED
    assert result.n_observations == 6
    assert result.gate_checks["minimum_sample_size"] is True
    assert result.gate_checks["beats_champion_by_margin"] is True


def test_challenger_does_not_clear_margin_rejects():
    # champion mean MAE = 2.0; margin=5% -> required <= 1.9; challenger mean MAE = 1.95 fails to clear it.
    result = _evaluate_gate(
        challenger_maes=[1.95, 1.95, 1.95, 1.95, 1.95, 1.95],
        champion_maes=[2.0, 2.0, 2.0, 2.0, 2.0, 2.0],
        min_evaluation_window=6,
        mae_improvement_margin=0.05,
    )

    assert result.verdict == PromotionVerdict.REJECTED
    assert result.n_observations == 6
    assert result.gate_checks["minimum_sample_size"] is True
    assert result.gate_checks["beats_champion_by_margin"] is False


def test_challenger_exactly_at_margin_boundary_promotes_inclusive():
    # champion mean MAE = 2.0; margin=5% -> required <= 1.9 exactly; challenger mean MAE = 1.9 (<=) passes.
    result = _evaluate_gate(
        challenger_maes=[1.9] * 6,
        champion_maes=[2.0] * 6,
        min_evaluation_window=6,
        mae_improvement_margin=0.05,
    )

    assert result.verdict == PromotionVerdict.PROMOTED
    assert result.gate_checks["beats_champion_by_margin"] is True


def test_mixed_history_uses_simple_unweighted_mean_across_the_whole_window():
    """A mixed history (some gameweeks the challenger wins big, some it loses) is decided by the plain
    mean across ALL qualifying gameweeks -- documented, tested aggregation rule, not a sustained-window
    or most-recent-N truncation."""
    # challenger: [0.5, 0.5, 0.5, 3.5, 3.5, 3.5] -> mean = 2.0
    # champion:   [2.0, 2.0, 2.0, 2.0, 2.0, 2.0] -> mean = 2.0
    # challenger mean (2.0) does NOT beat champion mean * 0.95 (1.9) -> REJECTED, not a coin flip.
    result = _evaluate_gate(
        challenger_maes=[0.5, 0.5, 0.5, 3.5, 3.5, 3.5],
        champion_maes=[2.0, 2.0, 2.0, 2.0, 2.0, 2.0],
        min_evaluation_window=6,
        mae_improvement_margin=0.05,
    )

    assert result.verdict == PromotionVerdict.REJECTED
    assert result.n_observations == 6


def test_more_than_minimum_window_observations_all_count_not_just_most_recent_n():
    """Extra history beyond min_evaluation_window is folded into the mean, not truncated away --
    documented in evaluate_promotion's own docstring."""
    # 8 observations total; if only the most recent 6 counted, this would be all 1.0s (PROMOTED
    # trivially). Using all 8 (including the two early 5.0 misses) pulls the mean up to 2.0, which
    # does NOT clear the 1.9 margin against a champion mean of 2.0.
    result = _evaluate_gate(
        challenger_maes=[5.0, 5.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
        champion_maes=[2.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0],
        min_evaluation_window=6,
        mae_improvement_margin=0.05,
    )

    assert result.n_observations == 8
    assert result.verdict == PromotionVerdict.REJECTED
