"""Milestone 3 Layer 3 baseline "models": pure lookups/formulas, no scikit-learn.

The four baseline model families (``packages/domain/modeling.py::ModelFamily``,
``BASELINE_MODEL_FAMILIES``) participate in walk-forward validation and
champion selection on equal footing with the tree-based candidates in
``candidates.py``, but none of them are actually fitted — each is a direct
lookup of an already-materialized ``player_features`` column, or (for
``predict_rules_based_ep_next``) one extra ``player_gameweek_stats`` lookup.
None gets a serialized artifact — a baseline's ``ModelVersion.artifact_uri``
always stays ``None`` (see ``app/jobs/handlers/model_training.py``).

``predict_rules_based_ep_next`` is a historical *backtesting proxy* for
Milestone 2's live ``app.optimization.projection.project_player`` formula
(``PROJECTION_FORMULA_VERSION = 1``: prefers ``ep_next``, falls back to
``points_per_game``). Neither of those two live ``players`` columns has a
per-gameweek-at-the-time historical analog on ``player_features`` — they are
both live, current-value-only fields, never snapshotted historically. FPL's
own ``xp_estimate`` column on ``player_gameweek_stats`` (ingested by
Milestone 3 Layer 1 — see that model's docstring) is the closest available
stand-in: it is FPL's own actual expected-points estimate for that player
going into that specific historical gameweek's deadline, which is exactly
the same kind of forward-looking, rules-based (non-learned) number
``ep_next`` is live. This is the only one of the four functions that touches
the database, since that value isn't itself a ``player_features`` column.
"""

from collections.abc import Sequence
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.database.models.player_gameweek_stats import PlayerGameweekStats

__all__ = [
    "FeatureRowLike",
    "predict_season_average",
    "predict_recent_average",
    "predict_weighted_recent_average",
    "predict_rules_based_ep_next",
    "sum_known_xp_estimates",
]


class FeatureRowLike(Protocol):
    """The subset of ``PlayerFeatures`` columns the pure-lookup baselines read.

    A ``Protocol`` (not the concrete ``PlayerFeatures`` ORM class) so unit
    tests can pass a lightweight hand-built stand-in without touching a
    database or constructing a full ORM instance.
    """

    # Read-only properties (not plain attributes) so structural matching is
    # covariant — a real PlayerFeatures row's ``Decimal | None`` columns
    # satisfy this Protocol's wider ``Decimal | float | None`` just fine,
    # which wouldn't type-check with plain (invariant) attribute annotations.
    @property
    def season_avg_points_to_date(self) -> Decimal | float | None: ...

    @property
    def career_avg_points_prior_seasons(self) -> Decimal | float | None: ...

    @property
    def rolling_avg_points_5gw(self) -> Decimal | float | None: ...

    @property
    def form_weighted_avg_points(self) -> Decimal | float | None: ...


def predict_season_average(feature_row: FeatureRowLike) -> float | None:
    """BASELINE_SEASON_AVERAGE: this season's average points-per-gameweek to date.

    Falls back to ``career_avg_points_prior_seasons`` when the player has no
    gameweeks played yet this season (e.g. this is gameweek 1, or the player
    is new to this season's squad but has prior-season history) — still
    ``None`` if neither is available (a genuinely brand-new player with no
    history anywhere).
    """
    if feature_row.season_avg_points_to_date is not None:
        return float(feature_row.season_avg_points_to_date)
    if feature_row.career_avg_points_prior_seasons is not None:
        return float(feature_row.career_avg_points_prior_seasons)
    return None


def predict_recent_average(feature_row: FeatureRowLike) -> float | None:
    """BASELINE_RECENT_AVERAGE: average points over the most recent 5 gameweeks with a row."""
    value = feature_row.rolling_avg_points_5gw
    return float(value) if value is not None else None


def predict_weighted_recent_average(feature_row: FeatureRowLike) -> float | None:
    """BASELINE_WEIGHTED_RECENT_AVERAGE: exponentially recency-weighted average points."""
    value = feature_row.form_weighted_avg_points
    return float(value) if value is not None else None


def sum_known_xp_estimates(xp_estimate_values: Sequence) -> float | None:
    """Sum whichever ``xp_estimate`` values are non-null across a set of rows.

    Shared by ``predict_rules_based_ep_next`` (one DB lookup per call) and
    the ``MODEL_TRAINING`` job handler's bulk-preloaded variant (one query
    up front for every player/gameweek pair a training run needs, to avoid
    an N+1 query per validation row). Returns ``None`` if given no rows at
    all, or if every row's ``xp_estimate`` is null — never coerced to ``0``.
    """
    if not xp_estimate_values:
        return None
    known = [float(value) for value in xp_estimate_values if value is not None]
    if not known:
        return None
    return sum(known)


def predict_rules_based_ep_next(session: Session, player_id: UUID, gameweek_id: UUID) -> float | None:
    """BASELINE_RULES_EP_NEXT: historical backtesting proxy for the live ep_next/points_per_game formula.

    Sums ``xp_estimate`` across every ``player_gameweek_stats`` row this
    player has for this (target) gameweek — mirroring how
    ``target_total_points`` itself is summed across a Double Gameweek's
    fixture rows (see ``app/features/engineering.py::sum_points_for_gameweek``).

    Returns ``None`` if the player has zero ``player_gameweek_stats`` rows
    for this gameweek at all, or if every row that does exist has a null
    ``xp_estimate`` (this column is absent from some historical seasons'
    source schema — see ``player_gameweek_stats``'s own module docstring —
    "no data" is never coerced to ``0``).
    """
    values: Sequence = (
        session.execute(
            select(PlayerGameweekStats.xp_estimate).where(
                PlayerGameweekStats.player_id == player_id,
                PlayerGameweekStats.gameweek_id == gameweek_id,
            )
        )
        .scalars()
        .all()
    )
    return sum_known_xp_estimates(values)
