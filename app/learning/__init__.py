"""Milestone 7 Layer 2: Recursive Learning Loop -- outcome ingestion + counterfactual engine.

``actuals.py`` -- pure DB-read primitives resolving real, already-known
``player_gameweek_stats`` outcomes.
``counterfactuals.py`` -- pure (no I/O) baseline-vs-alternative comparison
functions, mirroring ``app.h2h.simulate``/``app.chips.scenario``'s own
"no I/O in the computation module" discipline.
``outcome_ingestion.py`` -- the DB-session-taking orchestrator
(``ingest_outcome_for_audit``) that ties the two together into persisted
``recommendation_outcomes``/``counterfactual_results`` rows. Directly callable
today; wired into a real job (``GAMEWEEK_REVIEW``) by Milestone 7 Layer 4, not
built yet.

Milestone 7 Layer 3 (Recursive Learning Loop: Failure Attribution/Rolling
Learning Signals) adds:

``attribution.py`` -- ``classify_failure_attributions``/
``attribute_failures_for_outcome``, assigning zero-or-more ``FailureReasonCode``s
to a finished ``recommendation_outcome`` from its own ``counterfactual_results``
plus the original ``recommendation_audits`` row's generation-time context.
``signals.py`` -- ``compute_rolling_projection_mae``/
``compute_rolling_h2h_calibration``/``compute_rolling_chip_recommendation_value``/
``compute_failure_reason_concentration``, each a plain, directly-callable
rolling-window computation over already-persisted data. Both modules are
directly callable today, same as Layer 2's own building blocks; wiring them
into a real job is a later layer's concern (not built yet).
"""
