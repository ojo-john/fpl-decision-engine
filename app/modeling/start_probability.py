"""Start-probability classifier: feature matrix construction + label derivation + the classifier.

A SEPARATE, additive auxiliary model -- P(player starts this gameweek) -- never a change to the
main points-regression champion (``app/modeling/candidates.py``/``app/jobs/handlers
/model_training.py``). See ``app/jobs/handlers/start_probability_training.py``'s own module
docstring for the full "why an auxiliary model, not a retrain" rationale.

This exists to directly replace what's currently done by hand: manually-typed
``player_availability_risk_notes`` severities for rotation risk (e.g. real notes written this
season for Woltemade/Osula, reasoning from prose scouting reports about minutes trends) with a
learned signal from the same rolling-minutes/points history already computed for the main model.

Label definition
------------------
``START_LABEL_MINUTES_THRESHOLD`` (60 minutes) is the "did this player start and play most of the
match" proxy -- the same threshold widely used in football analytics as a stand-in for "started"
when only a per-gameweek minutes TOTAL is available (not a boolean "was in the starting XI" flag,
which this codebase's ``player_gameweek_stats`` schema does not carry). A Double Gameweek's two
fixture rows are SUMMED first (mirrors ``app.features.engineering.sum_points_for_gameweek``'s own
handling of the regression target, and ``rolling_avg_minutes``'s own per-gameweek aggregation) --
this is an intentional simplification: a player who starts and plays 45 minutes in EACH of two
legs (summed minutes >= 60) is labeled a "start" here even though no single match reached 60
minutes; documented here rather than silently assumed, since it is a real, if rare
(Double-Gameweek-only), edge case.

Feature selection, and why this is a CURATED subset of PlayerFeatures (not every column like the
main regression model's ``app.modeling.candidates._feature_record`` uses)
------------------------------------------------------------------------------------------------
Deliberately restricted to columns that are plausible, well-understood signals for "will this
player feature heavily in the next match" specifically -- recent minutes/points involvement,
recency-since-last-match, and position (goalkeepers/defenders are rotated far less than attacking
players in practice) -- rather than every column the main regression model uses (price, ownership,
fixture difficulty/home-away are properties of the MATCH's context or the wider transfer market,
not of whether THIS player personally gets picked to start it, and including them risked the
classifier picking up spurious correlations e.g. "expensive players start more" that aren't the
mechanism this auxiliary model is meant to isolate). If a future backtest run shows this curated
set underperforms a wider one, that is a candidate change for a later, evidence-driven revision --
not assumed here.
"""

from collections.abc import Sequence

import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

from app.modeling.candidates import KNOWN_POSITIONS
from packages.database.models.player_features import PlayerFeatures

__all__ = [
    "START_LABEL_MINUTES_THRESHOLD",
    "FEATURE_COLUMNS",
    "build_start_probability_feature_matrix",
    "build_start_probability_feature_row",
    "build_start_probability_classifier",
    "RANDOM_STATE",
    "GRADIENT_BOOSTING_MAX_ITER",
    "GRADIENT_BOOSTING_MAX_DEPTH",
    "GRADIENT_BOOSTING_LEARNING_RATE",
]

# See module docstring's "Label definition" section.
START_LABEL_MINUTES_THRESHOLD = 60

# See module docstring's "Feature selection" section for why this is a curated subset, not every
# PlayerFeatures column.
FEATURE_COLUMNS: tuple[str, ...] = (
    "rolling_avg_points_3gw",
    "rolling_avg_points_5gw",
    "rolling_avg_points_10gw",
    "rolling_avg_minutes_3gw",
    "rolling_avg_minutes_5gw",
    "season_avg_points_to_date",
    "rest_days",
    "is_missing_history",
)

# Hyperparameters, named module-level constants -- mirrors app.modeling.candidates' own
# "auditable, not a magic number inline" convention. HistGradientBoostingClassifier chosen for the
# exact same reason app.modeling.candidates chooses HistGradientBoostingRegressor: natively
# handles NaN features (a player's rolling-minutes/points history is frequently null early in
# their record) with no imputation, keeping this module consistent with the "null means genuinely
# unknown, never silently defaulted to 0" convention used throughout this codebase's feature
# engineering.
RANDOM_STATE = 42
GRADIENT_BOOSTING_MAX_ITER = 150
GRADIENT_BOOSTING_MAX_DEPTH = 4
GRADIENT_BOOSTING_LEARNING_RATE = 0.05


def _one_hot_position(position_short_name: str | None) -> dict[str, float]:
    """Deliberately duplicated (not imported) from app.modeling.candidates' own module-private
    ``_one_hot_position`` -- mirrors app.jobs.handlers.backtest's own established
    "module-private names aren't imported cross-module in this codebase" precedent (see that
    module's docstring)."""
    return {
        f"position_{position}": (1.0 if position_short_name == position else 0.0)
        for position in KNOWN_POSITIONS
    }


def _feature_record(row: PlayerFeatures) -> dict[str, float]:
    """Encode one ``PlayerFeatures`` row's CURATED feature subset (``FEATURE_COLUMNS``) into a
    plain ``dict``, plus position one-hot -- shared by
    ``build_start_probability_feature_matrix``/``build_start_probability_feature_row`` so training
    and live inference can never silently diverge in how a row's features are encoded, mirroring
    ``app.modeling.candidates``'s own identical "one shared record-builder" precedent."""
    record: dict[str, float] = {}
    for name in FEATURE_COLUMNS:
        value = getattr(row, name)
        record[name] = float(value) if value is not None else float("nan")
    record.update(_one_hot_position(row.position_short_name))
    return record


def build_start_probability_feature_matrix(
    feature_rows: Sequence[PlayerFeatures],
    minutes_by_player_gameweek: dict[tuple, int],
) -> tuple[pd.DataFrame, pd.Series]:
    """Build ``(X, y)`` for training/evaluating the start-probability classifier.

    Only rows where ``target_total_points is not None`` are included -- the target gameweek must
    actually be finished for a real label to exist, exactly mirroring
    ``app.modeling.candidates.build_feature_matrix``'s own playable-row filter (the SAME rows the
    main regression model trains against; this is not a different playability rule, just a
    different label extracted from the same rows).

    ``minutes_by_player_gameweek`` maps ``(player_id, gameweek_id) -> summed real minutes`` for
    that gameweek (see ``app/jobs/handlers/start_probability_training.py``'s bulk preload,
    mirroring ``app.jobs.handlers.model_training``'s own ``_preload_xp_estimates`` bulk-query
    pattern to avoid an N+1 query). A ``(player_id, gameweek_id)`` pair absent from this dict means
    zero ``player_gameweek_stats`` rows exist for it -- i.e. the player did not feature at all in a
    gameweek that HAS finished -- which is a real, valid ``0`` label (not missing data), exactly
    mirroring how ``target_total_points`` itself is ``0`` (never ``None``) for a finished gameweek
    a player didn't feature in (see ``app.features.engineering.sum_points_for_gameweek``'s own
    "empty sequence sums to 0" convention).
    """
    playable_rows = [row for row in feature_rows if row.target_total_points is not None]

    records = [_feature_record(row) for row in playable_rows]
    labels = []
    for row in playable_rows:
        minutes = minutes_by_player_gameweek.get((row.player_id, row.gameweek_id), 0)
        labels.append(1.0 if minutes >= START_LABEL_MINUTES_THRESHOLD else 0.0)

    x = pd.DataFrame.from_records(records)
    y = pd.Series(labels, name="started")
    return x, y


def build_start_probability_feature_row(feature_row: PlayerFeatures) -> pd.DataFrame:
    """Build a single-row feature matrix for LIVE inference -- no label, mirrors
    ``app.modeling.candidates.build_feature_row``'s own "live inference never filters on the
    (always-null, for a not-yet-played gameweek) label" reasoning exactly."""
    return pd.DataFrame.from_records([_feature_record(feature_row)])


def build_start_probability_classifier() -> HistGradientBoostingClassifier:
    """A fresh (unfitted) HistGradientBoostingClassifier -- natively handles NaN features, no
    imputation. See this module's own hyperparameter constants' docstring above."""
    return HistGradientBoostingClassifier(
        max_iter=GRADIENT_BOOSTING_MAX_ITER,
        max_depth=GRADIENT_BOOSTING_MAX_DEPTH,
        learning_rate=GRADIENT_BOOSTING_LEARNING_RATE,
        random_state=RANDOM_STATE,
    )
