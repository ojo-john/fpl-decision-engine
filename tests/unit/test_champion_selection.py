"""Unit tests for app.modeling.champion: aggregate metrics, champion selection, the single-champion invariant.

No database, no network — synthetic multi-candidate metric sets with a
known correct answer.
"""

from dataclasses import dataclass

import pytest

from app.modeling.champion import (
    CandidateMetrics,
    aggregate_mae,
    aggregate_spearman,
    select_champion,
    set_exactly_one_champion,
)

# --- aggregate_mae / aggregate_spearman ----------------------------------


def test_aggregate_mae_is_simple_mean_across_folds():
    assert aggregate_mae([1.0, 2.0, 3.0]) == pytest.approx(2.0)


def test_aggregate_spearman_averages_only_defined_folds():
    # one fold's spearman was undefined (None) -- averaged over the other two.
    assert aggregate_spearman([0.5, None, 0.7]) == pytest.approx(0.6)


def test_aggregate_spearman_none_when_every_fold_undefined():
    assert aggregate_spearman([None, None]) is None


# --- select_champion: lowest aggregate MAE wins --------------------------


def test_select_champion_picks_lowest_aggregate_mae():
    candidates = [
        CandidateMetrics(name="high_mae", fold_mae=[2.0, 2.0], fold_spearman=[0.5, 0.5]),
        CandidateMetrics(name="low_mae", fold_mae=[1.0, 1.0], fold_spearman=[0.1, 0.1]),
    ]
    assert select_champion(candidates) == "low_mae"


def test_select_champion_tie_break_on_highest_aggregate_spearman():
    # Both candidates tie on aggregate MAE (1.0) -- the one with higher
    # aggregate Spearman rank correlation must win the tie-break.
    candidates = [
        CandidateMetrics(name="lower_spearman", fold_mae=[1.0, 1.0], fold_spearman=[0.5, 0.7]),  # agg 0.6
        CandidateMetrics(name="higher_spearman", fold_mae=[1.0, 1.0], fold_spearman=[0.9, 0.9]),  # agg 0.9
    ]
    assert select_champion(candidates) == "higher_spearman"


def test_select_champion_undefined_spearman_sorts_as_worst_for_tiebreak_only():
    candidates = [
        CandidateMetrics(name="undefined_spearman", fold_mae=[1.0], fold_spearman=[None]),
        CandidateMetrics(name="defined_spearman", fold_mae=[1.0], fold_spearman=[0.1]),
    ]
    assert select_champion(candidates) == "defined_spearman"


def test_select_champion_mae_always_wins_outright_regardless_of_spearman():
    candidates = [
        CandidateMetrics(name="best_mae_worst_spearman", fold_mae=[1.0], fold_spearman=[None]),
        CandidateMetrics(name="worse_mae_best_spearman", fold_mae=[5.0], fold_spearman=[1.0]),
    ]
    assert select_champion(candidates) == "best_mae_worst_spearman"


def test_select_champion_requires_at_least_one_candidate():
    with pytest.raises(ValueError):
        select_champion([])


# --- set_exactly_one_champion: the invariant -----------------------------


@dataclass
class _Row:
    name: str
    is_champion: bool = False


def test_set_exactly_one_champion_flips_only_the_chosen_one_on():
    a, b, c = _Row("a", is_champion=True), _Row("b"), _Row("c")
    rows = [a, b, c]

    set_exactly_one_champion(rows, champion=b)

    assert [row.is_champion for row in rows] == [False, True, False]


def test_set_exactly_one_champion_reselection_flips_previous_champion_off():
    """A second candidate becoming champion must correctly un-champion the previous one."""
    a, b = _Row("a", is_champion=True), _Row("b", is_champion=False)
    rows = [a, b]

    set_exactly_one_champion(rows, champion=b)

    assert a.is_champion is False
    assert b.is_champion is True
    assert sum(1 for row in rows if row.is_champion) == 1
