"""Milestone 3 Layer 3 walk-forward validation: season-level expanding window fold construction.

Fold 1: train [earliest usable season]                  -> validate [next season]
Fold 2: train [earliest .. next season]                  -> validate [season after that]
...continuing through however many usable historical seasons actually exist
in this database right now.

Never shuffles; each candidate is refit fresh per fold (see
``app/jobs/handlers/model_training.py``). The list of usable seasons is
determined at runtime via a DB query against ``player_features`` (see
``usable_season_labels_with_features`` below) — never hardcoded — since a
fresh environment may only have a subset of historical seasons backfilled so
far.

Why this exists at all, given Layer 2 already prevents leakage
-----------------------------------------------------------------
Intra-season leakage was already prevented at the FEATURE level in Layer 2 —
a feature row's ``cutoff_timestamp`` guarantees it only aggregates
strictly-prior data (see ``player_features``'s own module docstring). This
fold structure exists purely to measure a DIFFERENT concern: cross-season
generalization — squad turnover between seasons, and rule changes (e.g. the
2025-26 season's ``defensive_contribution`` scoring introduction) that a
model trained on one season's dynamics may not transfer cleanly to the next.
Within-season leakage is already handled; this only measures across-season
generalization.

Graceful degradation with fewer than 2 usable seasons
-------------------------------------------------------
A real expanding-window fold structurally requires at least 2 distinct
seasons (something to train on, and a *later* season to validate
generalization against). A fresh environment may have only ONE historical
season backfilled (e.g. immediately after cloning, before a full multi-season
backfill has been run) — in that case ``build_expanding_window_folds``
returns a single degenerate fold that trains AND validates against that same
one season (in-sample; ``is_degenerate_single_season=True`` on the returned
``Fold``, so callers/reports can clearly label this as not a real
cross-season generalization measure) rather than raising or returning no
folds at all. Zero usable seasons still returns an empty list — there is
truly nothing to evaluate in that case.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.database.models.player_features import PlayerFeatures
from packages.database.models.season import Season

__all__ = ["Fold", "build_expanding_window_folds", "usable_season_labels_with_features"]


@dataclass(frozen=True)
class Fold:
    fold_index: int
    train_season_labels: tuple[str, ...]
    validation_season_label: str
    # True only for the single-season graceful-degradation case (see module
    # docstring) — the train and validation season are the same season, so
    # this fold measures in-sample fit, not cross-season generalization.
    is_degenerate_single_season: bool = False


def build_expanding_window_folds(ordered_season_labels: Sequence[str]) -> list[Fold]:
    """Build expanding-window folds from a chronologically-ordered list of season labels.

    - 0 seasons -> ``[]`` (nothing to evaluate).
    - 1 season -> a single degenerate fold training and validating against
      that same season (see module docstring's graceful-degradation note).
    - 2+ seasons -> one real fold per season starting from the second:
      fold ``i`` trains on ``ordered_season_labels[:i+1]`` and validates on
      ``ordered_season_labels[i+1]``.

    ``ordered_season_labels`` must already be in chronological order (e.g.
    from ``usable_season_labels_with_features``, which sorts them) — this
    function does not sort; never shuffles.
    """
    if not ordered_season_labels:
        return []
    if len(ordered_season_labels) == 1:
        only_season = ordered_season_labels[0]
        return [
            Fold(
                fold_index=0,
                train_season_labels=(only_season,),
                validation_season_label=only_season,
                is_degenerate_single_season=True,
            )
        ]

    folds = []
    for i in range(1, len(ordered_season_labels)):
        folds.append(
            Fold(
                fold_index=i - 1,
                train_season_labels=tuple(ordered_season_labels[:i]),
                validation_season_label=ordered_season_labels[i],
            )
        )
    return folds


def usable_season_labels_with_features(session: Session, feature_version: int) -> list[str]:
    """Every season label, chronologically sorted, with >=1 player_features row for this feature_version.

    Determined at runtime via a DB query, never hardcoded. Season labels
    (``"YYYY-YY"``) sort correctly as plain strings for any realistic year
    range, so a lexicographic sort is sufficient — no date parsing needed.
    """
    labels = (
        session.execute(
            select(Season.label)
            .join(PlayerFeatures, PlayerFeatures.season_id == Season.id)
            .where(PlayerFeatures.feature_version == feature_version)
            .distinct()
        )
        .scalars()
        .all()
    )
    return sorted(labels)
