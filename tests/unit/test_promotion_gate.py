"""Unit tests for app.modeling.promotion's pure gate-decision logic (``_evaluate_gate``).

No database, no network -- synthetic per-gameweek MAE pairs with a known
correct answer. Mirrors ``tests/unit/test_champion_selection.py``'s own
"synthetic multi-candidate metric sets" style.
"""

from app.modeling.promotion import _evaluate_gate, _per_position_gate_passes
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
    # per_position_segmentation defaults to passing when no per_position_mae_gap_pct is supplied
    # (no POSITION_SEGMENTATION_VALIDATION run exists yet for this feature_version) -- absence of
    # evidence never blocks a promotion for this gate, unlike minimum_sample_size.
    assert result.gate_checks["per_position_segmentation"] is True
    # historical_backtest_replay is the one gate this codebase genuinely cannot yet compute --
    # always reported as passing/not-applicable, never silently omitted.
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


# --- per_position_segmentation (real, computed gate -- see app.modeling.position_segmentation) ---


def test_per_position_gate_passes_when_no_validation_run_exists_yet():
    """Absence of a POSITION_SEGMENTATION_VALIDATION run is never treated as a failure -- this gate
    is informational until real evidence exists, unlike minimum_sample_size."""
    assert _per_position_gate_passes(None, mae_improvement_margin=0.05) is True
    assert _per_position_gate_passes({}, mae_improvement_margin=0.05) is True


def test_per_position_gate_passes_when_every_position_gap_is_none_or_below_margin():
    """A position with no defined gap (None -- e.g. zero validation rows for it that run) or a gap
    below the margin never fails the gate."""
    gaps = {"GKP": None, "DEF": 0.01, "MID": -0.10, "FWD": 0.049}
    assert _per_position_gate_passes(gaps, mae_improvement_margin=0.05) is True


def test_per_position_gate_fails_when_one_position_has_a_meaningful_gap():
    """A single position where a position-specific model would beat the global model by at least
    the margin is enough to fail the gate, even if every other position is fine."""
    gaps = {"GKP": 0.0, "DEF": 0.0, "MID": 0.0, "FWD": 0.10}
    assert _per_position_gate_passes(gaps, mae_improvement_margin=0.05) is False


def test_per_position_gate_fails_exactly_at_the_margin_boundary_exclusive():
    """Mirrors beats_champion_by_margin's own inclusive/exclusive convention -- but here a gap
    AT/ABOVE the margin means a real, meaningful improvement was found, so it must FAIL the gate
    (the gate is about flagging blind spots, not requiring them)."""
    assert _per_position_gate_passes({"FWD": 0.05}, mae_improvement_margin=0.05) is False
    assert _per_position_gate_passes({"FWD": 0.0499}, mae_improvement_margin=0.05) is True


def test_evaluate_gate_reports_real_per_position_result_alongside_other_gates():
    """The per_position_segmentation result flows through _evaluate_gate's own gate_checks exactly
    like the other two real gates -- verified end-to-end through the public function, not just the
    private helper."""
    result = _evaluate_gate(
        challenger_maes=[1.0] * 6,
        champion_maes=[2.0] * 6,
        min_evaluation_window=6,
        mae_improvement_margin=0.05,
        per_position_mae_gap_pct={"GKP": 0.0, "DEF": 0.0, "MID": 0.0, "FWD": 0.20},
    )
    assert result.gate_checks["per_position_segmentation"] is False
    # A real per-position blind spot does NOT, by itself, block an otherwise-clearing promotion --
    # it applies equally to the existing champion (same single-global-model architecture), so it's
    # informational, not a hard precondition the way minimum_sample_size is.
    assert result.verdict == PromotionVerdict.PROMOTED
