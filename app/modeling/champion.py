"""Milestone 3 Layer 3 champion selection: aggregate per-fold metrics, pick one winner.

No promotion-gate system yet (that's Milestone 7 scope) — this is a first,
simple "pick the best validated candidate" mechanism: after walk-forward
validation produces per-fold metrics for all 7 candidates (4 baselines + 3
tree models — see ``baselines.py``/``candidates.py``), each candidate's
AGGREGATE metric across folds is compared, and exactly one candidate is
flagged champion.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

__all__ = [
    "CandidateMetrics",
    "HasIsChampion",
    "aggregate_mae",
    "aggregate_spearman",
    "select_champion",
    "set_exactly_one_champion",
]


@dataclass(frozen=True)
class CandidateMetrics:
    """One candidate's per-fold metric values, already computed elsewhere (``metrics.py``)."""

    name: str
    fold_mae: Sequence[float]
    fold_spearman: Sequence[float | None]


def aggregate_mae(fold_mae: Sequence[float]) -> float:
    """Simple (unweighted) mean MAE across folds.

    A simple mean — not weighted by fold size — is used deliberately: each
    fold represents one full validation SEASON, and weighting by
    observation count would let a season with more players/gameweeks
    dominate the aggregate more than a season that happens to have slightly
    fewer rows. For a season-generalization metric, each season's fold
    should count equally regardless of its row count.
    """
    return sum(fold_mae) / len(fold_mae)


def aggregate_spearman(fold_spearman: Sequence[float | None]) -> float | None:
    """Mean Spearman rank correlation across folds where it was defined.

    ``None`` only if EVERY fold's Spearman was undefined (see
    ``app.modeling.metrics.spearman_rank_correlation``) — folds with a
    defined value are averaged over just those folds, never diluted by
    treating an undefined fold as 0.
    """
    known = [value for value in fold_spearman if value is not None]
    if not known:
        return None
    return sum(known) / len(known)


def select_champion(candidates: Sequence[CandidateMetrics]) -> str:
    """Select the champion's ``name``: lowest aggregate MAE (primary), highest aggregate Spearman (tie-break).

    MAE (absolute point-accuracy) is the primary criterion. Spearman rank
    correlation is the TIE-BREAK, not a co-primary criterion: when two
    candidates are statistically indistinguishable on MAE, ranking quality
    matters more than raw point accuracy for a squad optimizer that only
    cares about relative ordering within budget/position constraints (it
    picks WHO to buy, not an exact point total) — so Spearman breaks the
    tie.

    A candidate whose aggregate Spearman is undefined (``None``) sorts as if
    it had the worst possible Spearman for tie-break purposes only (it can
    still win outright on MAE).
    """
    if not candidates:
        raise ValueError("select_champion requires at least one candidate")

    def sort_key(candidate: CandidateMetrics) -> tuple[float, float]:
        mae = aggregate_mae(candidate.fold_mae)
        spearman = aggregate_spearman(candidate.fold_spearman)
        tie_break = spearman if spearman is not None else float("-inf")
        return (mae, -tie_break)

    return min(candidates, key=sort_key).name


class HasIsChampion(Protocol):
    """The one attribute ``set_exactly_one_champion`` needs — a mutable ``bool``.

    Deliberately a ``Protocol`` (not the concrete ``ModelVersion`` ORM
    class) so this function works identically against a list of real,
    already-loaded ``ModelVersion`` rows (mutating an ORM attribute marks it
    dirty for the next ``session.commit()`` — no separate DB write function
    needed) or a list of lightweight test doubles in a unit test.
    """

    is_champion: bool


def set_exactly_one_champion(candidates: Sequence[HasIsChampion], champion: HasIsChampion) -> None:
    """Mutate ``candidates`` in place so exactly one has ``is_champion=True``: ``champion``.

    Flips every OTHER candidate's ``is_champion`` to ``False`` first,
    implementing the "exactly one ``model_versions`` row has
    ``is_champion=True`` at any time" invariant explicitly rather than by
    convention. ``champion`` must be one of ``candidates`` (compared by
    identity) — callers pass the full, current set of rows (e.g. every
    ``model_versions`` row across every past training run, not just this
    run's 7), so a new champion correctly un-champions whatever the
    previous training run had flagged.
    """
    for candidate in candidates:
        candidate.is_champion = candidate is champion
