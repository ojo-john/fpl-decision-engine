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
"""
