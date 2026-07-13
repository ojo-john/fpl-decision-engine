"""Milestone 3 Layer 3: modeling/training for the ``MODEL_TRAINING`` job.

Direct single-target regression on ``total_points`` (not a staged
minutes -> events -> assembly pipeline) -- this fits the existing
single-scalar ``PlayerProjection`` interface (``app/optimization/projection.py``)
with zero changes needed there, and keeps this milestone's scope
proportionate: a staged architecture would need classification plus multiple
event sub-models plus an assembly formula, materially more complexity that
isn't justified until a much later milestone (the recursive self-improvement
loop) exists to make it pay for itself.

Modules:

- ``baselines.py`` -- 4 pure-lookup/formula "models" (no ``.fit()``, no
  artifact).
- ``candidates.py`` -- 3 real scikit-learn tree-based regressors, plus the
  shared feature-matrix construction they train against.
- ``walk_forward.py`` -- season-level expanding-window fold construction for
  cross-season generalization validation.
- ``metrics.py`` -- pure functions computing validation metrics over
  ``(y_true, y_pred)``.
- ``champion.py`` -- aggregates per-fold metrics and selects a single
  champion candidate.

Orchestrated end-to-end by ``app/jobs/handlers/model_training.py``.
"""
