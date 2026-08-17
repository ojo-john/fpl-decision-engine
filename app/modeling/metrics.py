"""Milestone 3 Layer 3 validation metrics: pure functions over ``(y_true, y_pred)``.

No database, no scikit-learn model objects — every function here takes
plain array-likes and returns a plain float (or ``None`` when the metric is
genuinely undefined for the given input, never a placeholder number like
``0``).
"""

from collections.abc import Sequence

import numpy as np
from scipy.stats import spearmanr
from sklearn.metrics import accuracy_score as _sklearn_accuracy
from sklearn.metrics import f1_score as _sklearn_f1
from sklearn.metrics import log_loss as _sklearn_log_loss
from sklearn.metrics import mean_absolute_error as _sklearn_mae
from sklearn.metrics import roc_auc_score as _sklearn_roc_auc
from sklearn.metrics import root_mean_squared_error as _sklearn_rmse

__all__ = [
    "mean_absolute_error",
    "root_mean_squared_error",
    "mean_bias",
    "spearman_rank_correlation",
    "top_k_capture_rate",
    "calibration_buckets",
    "binary_auc",
    "binary_accuracy",
    "binary_f1",
    "binary_log_loss",
]


def mean_absolute_error(y_true: Sequence[float], y_pred: Sequence[float]) -> float:
    """Mean absolute error — delegates to scikit-learn's well-tested implementation."""
    return float(_sklearn_mae(y_true, y_pred))


def root_mean_squared_error(y_true: Sequence[float], y_pred: Sequence[float]) -> float:
    """Root mean squared error — delegates to scikit-learn's well-tested implementation."""
    return float(_sklearn_rmse(y_true, y_pred))


def mean_bias(y_true: Sequence[float], y_pred: Sequence[float]) -> float:
    """Mean of ``(y_pred - y_true)`` — signed: positive means systematic overprediction."""
    true_arr = np.asarray(y_true, dtype=float)
    pred_arr = np.asarray(y_pred, dtype=float)
    return float(np.mean(pred_arr - true_arr))


def spearman_rank_correlation(y_true: Sequence[float], y_pred: Sequence[float]) -> float | None:
    """Spearman rank correlation between true and predicted values.

    Returns ``None`` if either array has fewer than 2 distinct values —
    rank correlation is undefined (not just "low") when there is no
    variation to rank in the first place. (``scipy.stats.spearmanr`` would
    otherwise silently return ``nan`` with a runtime warning in this case.)
    """
    true_arr = np.asarray(y_true, dtype=float)
    pred_arr = np.asarray(y_pred, dtype=float)
    if len(np.unique(true_arr)) < 2 or len(np.unique(pred_arr)) < 2:
        return None
    result = spearmanr(true_arr, pred_arr)
    return float(result.statistic)


def top_k_capture_rate(y_true: Sequence[float], y_pred: Sequence[float], k: int = 20) -> float | None:
    """Of the true top-``k`` players by actual value, what fraction are also in the predicted top-``k``?

    Returns ``None`` if fewer than ``k`` observations exist — top-k capture
    is undefined (not just "small") below that sample size.
    """
    true_arr = np.asarray(y_true, dtype=float)
    pred_arr = np.asarray(y_pred, dtype=float)
    n = len(true_arr)
    if n < k:
        return None
    # Descending order via argsort of the negated array; a stable sort keeps
    # tie-breaking deterministic (by original index) rather than arbitrary.
    true_top_k = set(np.argsort(-true_arr, kind="stable")[:k].tolist())
    pred_top_k = set(np.argsort(-pred_arr, kind="stable")[:k].tolist())
    return len(true_top_k & pred_top_k) / k


def binary_auc(y_true: Sequence[float], y_pred_proba: Sequence[float]) -> float | None:
    """ROC AUC for a binary classifier's predicted probabilities.

    ``None`` if ``y_true`` has fewer than 2 distinct classes -- AUC is undefined (not just "low")
    when there is only one class present in the evaluation set, exactly mirroring
    ``spearman_rank_correlation``'s own "undefined, not degenerate" convention above.
    """
    true_arr = np.asarray(y_true, dtype=float)
    if len(np.unique(true_arr)) < 2:
        return None
    return float(_sklearn_roc_auc(true_arr, np.asarray(y_pred_proba, dtype=float)))


def binary_accuracy(y_true: Sequence[float], y_pred_label: Sequence[float]) -> float:
    """Plain accuracy for already-thresholded binary predictions (0.0/1.0), not probabilities."""
    return float(_sklearn_accuracy(y_true, y_pred_label))


def binary_f1(y_true: Sequence[float], y_pred_label: Sequence[float]) -> float:
    """F1 score for already-thresholded binary predictions (0.0/1.0), not probabilities.

    ``zero_division=0`` -- a fold where the classifier predicts the positive class for nobody (or
    everybody) has a genuine, computable F1 of 0 rather than raising/warning, consistent with this
    module's "never raise on a degenerate-but-real evaluation set" convention.
    """
    return float(_sklearn_f1(y_true, y_pred_label, zero_division=0))


def binary_log_loss(y_true: Sequence[float], y_pred_proba: Sequence[float]) -> float | None:
    """Binary cross-entropy log loss between true labels and predicted probabilities.

    ``None`` if ``y_true`` has fewer than 2 distinct classes -- log loss against a single-class
    evaluation set is degenerate for the same reason ``binary_auc`` returns ``None`` there.
    """
    true_arr = np.asarray(y_true, dtype=float)
    if len(np.unique(true_arr)) < 2:
        return None
    return float(_sklearn_log_loss(true_arr, np.asarray(y_pred_proba, dtype=float)))


def calibration_buckets(y_true: Sequence[float], y_pred: Sequence[float], n_buckets: int = 5) -> dict:
    """Bucket observations into ``n_buckets`` equal-COUNT groups by predicted value; report each bucket mean.

    Equal-count (quantile) buckets are used rather than equal-width ranges:
    with a validation fold that can be small and FPL points are famously
    skewed (many 0s/1s, a long right tail), equal-width buckets over that
    range would often leave some buckets empty (no players predicted in
    that range) while a handful of buckets absorb almost all observations —
    equal-count buckets guarantee every bucket has a similar number of
    observations, so its mean is meaningfully comparable to the others.

    Observations are sorted ascending by predicted value, then split into
    ``n_buckets`` contiguous, near-equal-sized groups (``numpy.array_split``
    — a well-calibrated model has each bucket's ``mean_actual`` close to its
    ``mean_predicted``). Returns a dict keyed by bucket index (0 =
    lowest-predicted bucket), each value a dict of
    ``n``/``mean_predicted``/``mean_actual``/``pred_min``/``pred_max``.
    """
    true_arr = np.asarray(y_true, dtype=float)
    pred_arr = np.asarray(y_pred, dtype=float)
    order = np.argsort(pred_arr, kind="stable")
    sorted_true = true_arr[order]
    sorted_pred = pred_arr[order]

    buckets: dict[int, dict] = {}
    for bucket_index, idx in enumerate(np.array_split(np.arange(len(pred_arr)), n_buckets)):
        if len(idx) == 0:
            continue
        bucket_true = sorted_true[idx]
        bucket_pred = sorted_pred[idx]
        buckets[bucket_index] = {
            "n": int(len(idx)),
            "mean_predicted": float(np.mean(bucket_pred)),
            "mean_actual": float(np.mean(bucket_true)),
            "pred_min": float(np.min(bucket_pred)),
            "pred_max": float(np.max(bucket_pred)),
        }
    return buckets
