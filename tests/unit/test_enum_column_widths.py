"""Guard every ``native_enum=False`` enum-backed column's real database width.

Real, recurring bug class (found three separate times this project's lifetime --
``ingestion_runs.run_type`` in migration 0006, ``jobs.job_type``/``status``/``trigger_type`` in
migration 0041, and 39 more columns across the whole schema in migration 0043): a
``native_enum=False`` ``SAEnum(SomeEnum, ...)`` column with no explicit ``length=`` gets its real
database width fixed by Alembic/SQLAlchemy at whatever ``SomeEnum``'s longest member was the
FIRST TIME its table's creating migration ever ran against a given database -- NOT automatically
kept in sync with later additions to that enum. A fresh database created via
``alembic upgrade head`` today auto-sizes the column against the CURRENT enum and never shows
this bug; only an already-migrated database (like the real dev database) can silently have a
column too narrow for what the enum needs right now. This is exactly why the bug was invisible
to every integration test run this session (a fresh test database on every run) and only ever
surfaced against the real, persistent dev database.

This test is the permanent fix: it dynamically discovers EVERY ``native_enum=False`` column
across the entire ORM metadata (so it automatically covers future new tables/columns too, not
just today's), and requires each one to have an explicit entry in ``_REAL_COLUMN_WIDTHS`` below.

Two ways this test can fail, both intentional:
1. A column's real registered width is too narrow for its enum's CURRENT longest member (an enum
   gained a new, longer value after the width was last chosen) -- fix by widening the real
   column via a new migration, then updating the width recorded here.
2. A NEW ``native_enum=False`` enum column was added to the schema without a corresponding entry
   here -- fix by adding the real migration setting its width, then adding it to
   ``_REAL_COLUMN_WIDTHS`` (this failure mode is deliberate: it forces whoever adds a new
   enum-backed column to consciously pick and record a real width, rather than silently trusting
   whatever Alembic happens to auto-compute the first time the migration runs).

See ``tests/unit/test_job_enums.py``'s own, narrower ``jobs``-table-specific precedent (kept as
its own test, not removed -- this one generalizes the same idea across the whole schema).
"""

from enum import Enum as PyEnum

from sqlalchemy import Enum as SAEnum

import packages.database.models  # noqa: F401 -- import for side effect: registers every model with Base.metadata
from packages.database.base import Base

# Real width chosen for each (table, column) as of migration 0043 (or an earlier migration, for
# the handful of columns already widened before this audit -- see each migration's own docstring
# for the real reasoning behind columns that AREN'T uniformly 64).
_REAL_COLUMN_WIDTHS: dict[tuple[str, str], int] = {
    ("backtest_runs", "run_status"): 64,
    ("chip_optimization_runs", "rules_status_snapshot"): 64,
    ("chip_optimization_runs", "run_status"): 64,
    # chip_type columns: already had real headroom (14 vs enum max 8) from whatever migration
    # first created them -- left untouched by 0043, not silently broken, not touched.
    ("chip_recommendations", "chip_type"): 14,
    ("chip_recommendations", "verdict"): 64,
    ("chip_window_candidates", "chip_type"): 14,
    ("cold_start_validation_runs", "run_status"): 64,
    ("counterfactual_results", "comparison_type"): 64,
    ("data_quality_issues", "severity"): 64,
    ("failure_attributions", "category"): 64,
    ("failure_attributions", "reason_code"): 64,
    ("h2h_simulation_runs", "confidence"): 64,
    ("h2h_simulation_runs", "rules_status_snapshot"): 64,
    ("h2h_simulation_runs", "run_status"): 64,
    ("h2h_strategy_candidates", "strategy_label"): 64,
    ("ingestion_runs", "overall_health"): 64,
    # Widened to 30 by migration 0006 specifically for IngestionRunType -- real headroom (30 vs
    # enum max 25) already, left untouched by 0043.
    ("ingestion_runs", "run_type"): 30,
    ("ingestion_runs", "status"): 64,
    # Widened to 50 by migration 0041 specifically -- real headroom (50 vs enum max 32) already,
    # left untouched by 0043.
    ("jobs", "job_type"): 50,
    ("jobs", "status"): 50,
    ("jobs", "trigger_type"): 50,
    ("league_sync_runs", "fpl_api_health_snapshot"): 64,
    ("league_sync_runs", "run_status"): 64,
    ("leagues", "league_type"): 64,
    ("learning_reports", "run_status"): 64,
    ("learning_signals", "signal_type"): 64,
    ("manager_squad_snapshots", "source"): 64,
    ("model_promotions", "verdict"): 64,
    ("model_versions", "lifecycle_state"): 64,
    ("model_versions", "model_family"): 64,
    # Already had real headroom (30 vs enum max 18) from whatever migration first created it --
    # left untouched by 0043.
    ("optimization_runs", "recommendation_type"): 30,
    ("optimization_runs", "rules_status_snapshot"): 64,
    ("optimization_runs", "run_status"): 64,
    ("optimization_runs", "solver_status"): 64,
    ("optimization_runs", "target_gameweek_status"): 64,
    ("position_segmentation_validation_runs", "run_status"): 64,
    ("raw_api_snapshots", "health_status"): 64,
    ("recommendation_audits", "confidence"): 64,
    ("recommendation_audits", "fpl_api_health_at_generation"): 64,
    ("rival_gameweek_snapshots", "picks_availability"): 64,
    ("rival_profiles", "derived_free_transfers_confidence"): 64,
    ("rival_profiles", "overall_confidence"): 64,
    ("rules_config", "rules_status"): 64,
    ("rules_config_fields", "confidence"): 64,
    ("transfer_optimization_runs", "rules_status_snapshot"): 64,
    ("transfer_optimization_runs", "run_status"): 64,
}


def _discover_enum_columns() -> list[tuple[str, str, type[PyEnum]]]:
    """Every ``native_enum=False`` ``SAEnum`` column currently registered on ``Base.metadata``,
    as ``(table_name, column_name, enum_class)`` -- dynamic, so a brand-new model file
    automatically gets covered the moment it's imported, with no separate registration step."""
    found = []
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if isinstance(col.type, SAEnum) and not col.type.native_enum and col.type.enum_class is not None:
                found.append((table.name, col.name, col.type.enum_class))
    return found


def test_every_enum_column_has_a_registered_real_width():
    """Fails loudly the moment a new native_enum=False column is added without a real,
    consciously-chosen width recorded above -- see this module's own docstring for why that's
    the point, not a limitation."""
    discovered = {(t, c) for t, c, _ in _discover_enum_columns()}
    registered = set(_REAL_COLUMN_WIDTHS)
    missing = discovered - registered
    assert not missing, (
        f"New enum-backed column(s) found with no registered real width: {sorted(missing)}. "
        "Add a real migration widening it (see migration 0043 for the pattern) and register the "
        "chosen width in _REAL_COLUMN_WIDTHS above."
    )
    # The reverse (registered but no longer discovered, e.g. a column/table was dropped) is not
    # asserted as a failure -- stale entries here are harmless, just dead weight to clean up
    # eventually, not a real risk the way a MISSING entry is.


def test_every_enum_column_fits_its_real_registered_width():
    for table_name, column_name, enum_cls in _discover_enum_columns():
        width = _REAL_COLUMN_WIDTHS.get((table_name, column_name))
        if width is None:
            continue  # already reported by test_every_enum_column_has_a_registered_real_width
        max_len = max(len(member.value) for member in enum_cls)
        longest_member = max((m for m in enum_cls), key=lambda m: len(m.value))
        assert max_len <= width, (
            f"{table_name}.{column_name}: {enum_cls.__name__}.{longest_member.name} is "
            f"{max_len} chars -- exceeds its registered real column width ({width}). Add a "
            "migration widening the real column, then update the width recorded above."
        )
