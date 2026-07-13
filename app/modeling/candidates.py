"""Milestone 3 Layer 3 tree-based candidates: feature matrix construction + 3 scikit-learn regressors.

Three scikit-learn regressor families are evaluated alongside the 4
pure-lookup baselines (``baselines.py``) during walk-forward validation:

- ``DecisionTreeRegressor`` — shallow, chosen for interpretability.
- ``RandomForestRegressor`` — a reasonable default ensemble, not exhaustively
  tuned.
- ``HistGradientBoostingRegressor`` — chosen over plain
  ``GradientBoostingRegressor`` specifically because it natively handles NaN
  features without imputation. Rolling-window features
  (``rolling_avg_points_3gw`` and similar) are frequently null early in a
  player's history, and this codebase's convention throughout Layers 1-2 is
  "null means genuinely unknown, never silently defaulted to 0" — imputing
  nulls to 0 before fitting would violate that convention and would also be
  actively misleading (a player with no rolling history is not the same as
  a player who averages 0 points). Verified directly (see this module's
  unit tests / the Layer 3 validation pass) that scikit-learn 1.9's
  ``DecisionTreeRegressor``/``RandomForestRegressor`` also handle NaN
  splits natively on this version, so no imputation happens anywhere in
  this module for any of the three candidates.

Hyperparameters are named module-level constants (not magic numbers inline)
so Layer 3's choices are visible/auditable and easy to revisit later without
spelunking through training-loop code.
"""

from collections.abc import Sequence

import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor, RandomForestRegressor
from sklearn.tree import DecisionTreeRegressor

from packages.database.models.player_features import PlayerFeatures

__all__ = [
    "IDENTIFIER_LABEL_AUDIT_COLUMNS",
    "KNOWN_POSITIONS",
    "build_feature_matrix",
    "build_feature_row",
    "select_non_degenerate_columns",
    "align_columns",
    "build_decision_tree",
    "build_random_forest",
    "build_gradient_boosting",
    "DECISION_TREE_MAX_DEPTH",
    "RANDOM_FOREST_N_ESTIMATORS",
    "RANDOM_FOREST_MAX_DEPTH",
    "GRADIENT_BOOSTING_MAX_ITER",
    "GRADIENT_BOOSTING_MAX_DEPTH",
    "GRADIENT_BOOSTING_LEARNING_RATE",
    "RANDOM_STATE",
]

# Identifiers, the label itself, and audit/bookkeeping columns that carry no
# predictive signal (and would leak trivially if included — e.g.
# computed_at correlating with row insertion order) are excluded from the
# feature matrix entirely.
IDENTIFIER_LABEL_AUDIT_COLUMNS = frozenset(
    {
        "id",
        "player_id",
        "season_id",
        "gameweek_id",
        "feature_version",
        "cutoff_timestamp",
        "computed_at",
        "target_total_points",
        "data_source",
        "created_at",
        "updated_at",
    }
)

# Booleans in the DB, encoded as a plain 0.0/1.0/NaN numeric feature (NaN for
# an unset next_fixture_was_home — no next fixture resolved for that
# gameweek — never coerced to False/0).
_BOOLEAN_FEATURE_COLUMNS = frozenset({"next_fixture_was_home", "is_missing_history"})

# position_short_name is one-hot encoded explicitly against this fixed,
# known set (rather than via pandas.get_dummies over whatever categories
# happen to appear in a given split) so the resulting feature matrix always
# has the exact same columns regardless of which positions are present in a
# particular walk-forward fold's train/validation seasons — critical since a
# tree fit on one fold's columns must line up with the next fold's. A
# position outside this known set (including a null position) is encoded as
# all-zeros across every position_* column, the standard one-hot "unknown
# falls to baseline" convention.
KNOWN_POSITIONS = ("GKP", "DEF", "MID", "FWD")

# --- Hyperparameters (module-level, named constants — not tuned to death for
# this milestone's scope; revisit alongside FEATURE_VERSION if a later
# milestone's recursive self-improvement loop finds better values) ---------

RANDOM_STATE = 42

# Shallow — chosen for interpretability over raw accuracy.
DECISION_TREE_MAX_DEPTH = 5

RANDOM_FOREST_N_ESTIMATORS = 200
RANDOM_FOREST_MAX_DEPTH = 8

GRADIENT_BOOSTING_MAX_ITER = 200
GRADIENT_BOOSTING_MAX_DEPTH = 5
GRADIENT_BOOSTING_LEARNING_RATE = 0.05


def _one_hot_position(position_short_name: str | None) -> dict[str, float]:
    return {
        f"position_{position}": (1.0 if position_short_name == position else 0.0)
        for position in KNOWN_POSITIONS
    }


def _feature_record(row: PlayerFeatures) -> dict[str, float]:
    """Encode one ``PlayerFeatures`` row's feature columns into a plain ``dict``.

    Shared by ``build_feature_matrix`` (training/evaluation, many rows) and
    ``build_feature_row`` (live inference, exactly one row) so the two can
    never silently diverge in how a row's features are encoded — only
    ``build_feature_matrix``'s row-level ``target_total_points`` filtering is
    specific to the training/evaluation context (see that function's
    docstring vs. ``build_feature_row``'s).
    """
    record: dict[str, float] = {}
    for column in PlayerFeatures.__table__.columns:
        name = column.name
        if name in IDENTIFIER_LABEL_AUDIT_COLUMNS or name == "position_short_name":
            continue
        value = getattr(row, name)
        record[name] = float(value) if value is not None else float("nan")
    record.update(_one_hot_position(row.position_short_name))
    return record


def build_feature_matrix(feature_rows: Sequence[PlayerFeatures]) -> tuple[pd.DataFrame, pd.Series]:
    """Build ``(X, y)`` for training/evaluating a tree-based candidate against a list of ``PlayerFeatures``.

    Rows where ``target_total_points`` is NULL (the target gameweek hasn't
    been played yet) are EXCLUDED entirely — there is no label to train or
    evaluate against for those rows. Filtering happens here, once, so every
    caller (walk-forward validation, the job handler) gets the same
    guarantee rather than each having to remember to filter separately.

    Every other numeric feature column's nulls pass through as NaN, never
    imputed to 0 or any other value (see this module's docstring).
    ``position_short_name`` is one-hot encoded against ``KNOWN_POSITIONS``;
    ``is_missing_history`` is included as a plain 0.0/1.0 feature.
    """
    playable_rows = [row for row in feature_rows if row.target_total_points is not None]

    records = [_feature_record(row) for row in playable_rows]

    x = pd.DataFrame.from_records(records)
    y = pd.Series(
        [float(row.target_total_points) for row in playable_rows if row.target_total_points is not None],
        name="target_total_points",
    )
    return x, y


def build_feature_row(feature_row: PlayerFeatures) -> pd.DataFrame:
    """Build a single-row feature matrix for LIVE inference — deliberately no ``target_total_points`` filter.

    ``build_feature_matrix`` excludes any row with a NULL label, which is
    correct for training/evaluation (there is nothing to score against) but
    wrong for live inference: a projection is always for a gameweek that
    hasn't been played yet, so ``target_total_points`` is *always* NULL on
    the row being projected — that is the expected, normal state, not
    invalid data, and must never cause the row to be silently dropped
    (which would look like "no prediction" rather than the truth, "not
    playable yet"). Reuses the exact same per-row feature-encoding logic as
    ``build_feature_matrix`` (``_feature_record``) so live inference and
    training features are computed identically — this function only differs
    in skipping the label-based row filter.
    """
    return pd.DataFrame.from_records([_feature_record(feature_row)])


def select_non_degenerate_columns(x_train: pd.DataFrame) -> list[str]:
    """Return ``x_train`` columns with at least 2 distinct non-null values.

    A column that's constant (or entirely null) across every training row
    carries no signal for any of the 3 tree candidates and must be dropped
    before fitting -- found live: ``HistGradientBoostingRegressor``'s
    internal binning (``numpy.lib.stride_tricks.sliding_window_view``) raises
    ``ValueError: window shape cannot be larger than input array shape`` for
    any column with fewer than 2 distinct values, since it can't form a
    length-2 sliding window over 0 or 1 distinct values. This isn't specific
    to that one estimator or to small fixture datasets -- any real feature
    set can have a degenerate column in some season/fold (e.g. a position
    with no representation, or a feature not yet populated for a brand-new
    ``feature_version``), so this applies to all 3 candidates uniformly.

    The caller must apply this SAME column selection to every other matrix
    derived from the same fit (e.g. a fold's validation matrix) via
    ``align_columns`` below -- never independently recomputed per matrix.
    """
    return [column for column in x_train.columns if x_train[column].nunique(dropna=True) >= 2]


def align_columns(x: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """Reindex ``x`` to exactly ``columns`` (filling any missing one-hot column with 0.0, not NaN).

    Used to make a validation/prediction matrix match the exact column set a
    model was fit on (see ``select_non_degenerate_columns``) -- a one-hot
    position column absent from ``x`` (e.g. a position with no players in
    this particular fold) is filled with 0.0 (the correct one-hot "absent"
    value), while any other genuinely-missing feature is filled with NaN
    (the same "unknown, not zero" convention used everywhere else in this
    module), never silently defaulted to 0.
    """
    missing = [column for column in columns if column not in x.columns]
    result = x.reindex(columns=columns)
    for column in missing:
        if column.startswith("position_"):
            result[column] = 0.0
    return result


def build_decision_tree() -> DecisionTreeRegressor:
    """A shallow, fresh (unfitted) DecisionTreeRegressor — chosen for interpretability."""
    return DecisionTreeRegressor(max_depth=DECISION_TREE_MAX_DEPTH, random_state=RANDOM_STATE)


def build_random_forest() -> RandomForestRegressor:
    """A fresh (unfitted) RandomForestRegressor with reasonable, not exhaustively-tuned defaults."""
    return RandomForestRegressor(
        n_estimators=RANDOM_FOREST_N_ESTIMATORS,
        max_depth=RANDOM_FOREST_MAX_DEPTH,
        random_state=RANDOM_STATE,
        n_jobs=-1,
    )


def build_gradient_boosting() -> HistGradientBoostingRegressor:
    """A fresh (unfitted) HistGradientBoostingRegressor — natively handles NaN features, no imputation."""
    return HistGradientBoostingRegressor(
        max_iter=GRADIENT_BOOSTING_MAX_ITER,
        max_depth=GRADIENT_BOOSTING_MAX_DEPTH,
        learning_rate=GRADIENT_BOOSTING_LEARNING_RATE,
        random_state=RANDOM_STATE,
    )
