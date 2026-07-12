"""Integration tests for Alembic migrations against a real Postgres database.

Requires a running Postgres (see conftest.py docstring). Runs the actual
`alembic upgrade head` / `alembic downgrade base` commands via subprocess so
this exercises exactly what CI and operators run.
"""

import subprocess
import sys
from pathlib import Path

from sqlalchemy import inspect, text

REPO_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_COLUMNS = {
    "id",
    "job_type",
    "requested_by",
    "trigger_type",
    "status",
    "parameters_json",
    "scheduled_for",
    "started_at",
    "completed_at",
    "attempt_number",
    "idempotency_key",
    "git_commit_sha",
    "application_version",
    "error_code",
    "error_message",
    "output_json",
    "logs_uri",
    "created_at",
    "updated_at",
}

EXPECTED_TABLE_COLUMNS = {
    "jobs": EXPECTED_COLUMNS,
    "seasons": {"id", "label", "is_current", "created_at", "updated_at"},
    "ingestion_runs": {
        "id",
        "job_id",
        "run_type",
        "started_at",
        "completed_at",
        "status",
        "overall_health",
        "data_quality_score",
        "optimization_permitted",
        "report_markdown",
        "summary_json",
        "created_at",
        "updated_at",
    },
    "raw_api_snapshots": {
        "id",
        "ingestion_run_id",
        "endpoint",
        "request_url",
        "request_params_json",
        "retrieved_at",
        "http_status",
        "attempt_count",
        "latency_ms",
        "health_status",
        "checksum_sha256",
        "schema_signature",
        "schema_drift_detected",
        "raw_body",
        "error_message",
        "created_at",
    },
    "rules_config": {
        "id",
        "season_id",
        "version_number",
        "source_ingestion_run_id",
        "checksum_sha256",
        "raw_rules_json",
        "raw_scoring_json",
        "raw_chips_json",
        "raw_element_types_json",
        "rules_status",
        "is_current",
        "effective_from",
        "superseded_at",
        "last_confirmed_at",
        "change_summary_markdown",
        "created_at",
        "updated_at",
    },
    "rules_config_fields": {
        "id",
        "rules_config_id",
        "field_path",
        "value_json",
        "confidence",
        "source",
        "notes",
        "created_at",
    },
    "data_quality_issues": {
        "id",
        "ingestion_run_id",
        "entity_type",
        "entity_fpl_id",
        "severity",
        "check_name",
        "message",
        "created_at",
    },
    "teams": {
        "id",
        "fpl_team_id",
        "season_id",
        "code",
        "name",
        "short_name",
        "strength",
        "strength_overall_home",
        "strength_overall_away",
        "strength_attack_home",
        "strength_attack_away",
        "strength_defence_home",
        "strength_defence_away",
        "played",
        "win",
        "draw",
        "loss",
        "points",
        "position",
        "unavailable",
        "last_synced_at",
        "created_at",
        "updated_at",
    },
    "positions": {
        "id",
        "fpl_element_type_id",
        "season_id",
        "singular_name",
        "short_name",
        "squad_select",
        "squad_min_play",
        "squad_max_play",
        "created_at",
        "updated_at",
    },
    "players": {
        "id",
        "fpl_element_id",
        "fpl_code",
        "season_id",
        "team_id",
        "position_id",
        "first_name",
        "second_name",
        "web_name",
        "now_cost",
        "status",
        "total_points",
        "minutes",
        "goals_scored",
        "assists",
        "clean_sheets",
        "goals_conceded",
        "own_goals",
        "penalties_saved",
        "penalties_missed",
        "yellow_cards",
        "red_cards",
        "saves",
        "bonus",
        "bps",
        "selected_by_percent",
        "chance_of_playing_next_round",
        "chance_of_playing_this_round",
        "points_per_game",
        "form",
        "ep_this",
        "ep_next",
        "starts",
        "news",
        "is_quarantined",
        "quarantine_reason",
        "last_synced_at",
        "created_at",
        "updated_at",
    },
    "gameweeks": {
        "id",
        "fpl_event_id",
        "season_id",
        "name",
        "deadline_time",
        "finished",
        "data_checked",
        "is_previous",
        "is_current",
        "is_next",
        "average_entry_score",
        "highest_score",
        "last_synced_at",
        "created_at",
        "updated_at",
    },
    "fixtures": {
        "id",
        "fpl_fixture_id",
        "season_id",
        "gameweek_id",
        "home_team_id",
        "away_team_id",
        "kickoff_time",
        "finished",
        "started",
        "home_score",
        "away_score",
        "home_difficulty",
        "away_difficulty",
        "is_quarantined",
        "quarantine_reason",
        "last_synced_at",
        "created_at",
        "updated_at",
    },
}

EXPECTED_MILESTONE_2_TABLE_COLUMNS = {
    "optimization_runs": {
        "id",
        "job_id",
        "season_id",
        "rules_config_id",
        "recommendation_type",
        "source_optimization_run_id",
        "run_status",
        "solver_status",
        "target_gameweek_id",
        "target_gameweek_status",
        "squad_player_ids",
        "starting_xi_player_ids",
        "bench_player_ids_ordered",
        "captain_player_id",
        "vice_captain_player_id",
        "squad_total_cost",
        "squad_projected_points",
        "xi_projected_points",
        "optimization_permitted_snapshot",
        "data_quality_score_snapshot",
        "rules_status_snapshot",
        "projection_formula_version",
        "error_code",
        "error_message",
        "parameters_json",
        "started_at",
        "completed_at",
        "created_at",
        "updated_at",
    },
    "optimization_run_players": {
        "id",
        "optimization_run_id",
        "player_id",
        "is_starting",
        "bench_order",
        "is_captain",
        "is_vice_captain",
        "projected_points",
        "projection_source",
        "now_cost_snapshot",
        "created_at",
        "updated_at",
    },
}


EXPECTED_MILESTONE_3_LAYER1_TABLE_COLUMNS = {
    "player_gameweek_stats": {
        "id",
        "player_id",
        "season_id",
        "gameweek_id",
        "fixture_id",
        "opponent_team_id",
        "was_home",
        "position_short_name_at_time",
        "minutes",
        "total_points",
        "goals_scored",
        "assists",
        "clean_sheets",
        "goals_conceded",
        "own_goals",
        "penalties_saved",
        "penalties_missed",
        "yellow_cards",
        "red_cards",
        "saves",
        "bonus",
        "bps",
        "ict_index",
        "influence",
        "creativity",
        "threat",
        "expected_goals",
        "expected_assists",
        "expected_goal_involvements",
        "expected_goals_conceded",
        "starts",
        "defensive_contribution",
        "clearances_blocks_interceptions",
        "recoveries",
        "tackles",
        "xp_estimate",
        "value_at_time",
        "selected_by_count_at_time",
        "transfers_in",
        "transfers_out",
        "transfers_balance",
        "data_source",
        "extra_stats_json",
        "is_quarantined",
        "quarantine_reason",
        "created_at",
        "updated_at",
    },
}


def _run_alembic(*args: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        check=True,
    )


def _clear_optimization_runs(engine) -> None:
    """Delete all ``optimization_runs`` rows (cascades to ``optimization_run_players``).

    Migration 0005's downgrade narrows ``recommendation_type`` back to
    ``VARCHAR(13)`` — if any other integration test already persisted a
    ``JOINT_SQUAD_AND_XI`` row (19 chars) on this shared, non-ephemeral test
    database, that narrowing fails outright (Postgres raises
    ``StringDataRightTruncation`` rather than silently truncating). These
    tests don't care about that data — the tables get dropped entirely a step
    or two later in the same downgrade chain — so it's safe to clear it first.
    """
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM optimization_runs"))


def _clear_ingestion_runs(engine) -> None:
    """Delete all ``ingestion_runs`` rows (cascades to raw_api_snapshots/data_quality_issues).

    Migration 0006's downgrade narrows ``ingestion_runs.run_type`` back to
    ``VARCHAR(14)`` — if any other integration test already persisted a
    ``'historical_data_ingestion'`` run_type row (25 chars) on this shared,
    non-ephemeral test database, that narrowing fails outright (Postgres
    raises ``StringDataRightTruncation``). Same rationale as
    ``_clear_optimization_runs`` above.

    ``rules_config.source_ingestion_run_id`` also references ``ingestion_runs.id``
    (nullably, without ``ondelete=CASCADE``) — nulled out first so the delete
    below doesn't fail with a foreign key violation.
    """
    with engine.begin() as conn:
        conn.execute(text("UPDATE rules_config SET source_ingestion_run_id = NULL"))
        conn.execute(text("DELETE FROM ingestion_runs"))


def test_upgrade_creates_milestone_3_layer1_tables_with_expected_columns(require_database):
    """Covers ``player_gameweek_stats`` and the ``ingestion_runs.run_type`` widening (Milestone 3 Layer 1)."""
    from packages.database.session import engine

    _run_alembic("upgrade", "head")
    try:
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        for table_name, expected_columns in EXPECTED_MILESTONE_3_LAYER1_TABLE_COLUMNS.items():
            assert table_name in table_names, f"expected table {table_name!r} to exist after upgrade head"
            columns = {col["name"] for col in inspector.get_columns(table_name)}
            assert expected_columns.issubset(columns), (
                f"{table_name} missing columns: {expected_columns - columns}"
            )

        _clear_ingestion_runs(engine)
        _run_alembic("downgrade", "0005")
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        for table_name in EXPECTED_MILESTONE_3_LAYER1_TABLE_COLUMNS:
            assert table_name not in table_names, (
                f"expected table {table_name!r} to be gone after downgrade to 0005"
            )
    finally:
        _run_alembic("upgrade", "head")


def test_upgrade_creates_milestone_2_tables_with_expected_columns(require_database):
    """Covers ``optimization_runs``/``optimization_run_players``, added in Milestone 2."""
    from packages.database.session import engine

    _run_alembic("upgrade", "head")
    try:
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        for table_name, expected_columns in EXPECTED_MILESTONE_2_TABLE_COLUMNS.items():
            assert table_name in table_names, f"expected table {table_name!r} to exist after upgrade head"
            columns = {col["name"] for col in inspector.get_columns(table_name)}
            assert expected_columns.issubset(columns), (
                f"{table_name} missing columns: {expected_columns - columns}"
            )

        _clear_optimization_runs(engine)
        _clear_ingestion_runs(engine)
        _run_alembic("downgrade", "0003")
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        for table_name in EXPECTED_MILESTONE_2_TABLE_COLUMNS:
            assert table_name not in table_names, (
                f"expected table {table_name!r} to be gone after downgrade to 0003"
            )
    finally:
        _run_alembic("upgrade", "head")


def test_upgrade_creates_jobs_table_with_expected_columns(require_database):
    from packages.database.session import engine

    # This test intentionally downgrades to base to prove `downgrade` actually
    # drops the table, then re-upgrades in a `finally` so the shared test
    # database is always left at `head` for other integration tests running
    # in the same session — without this, test order would silently determine
    # whether `jobs` exists when other test modules run.
    _run_alembic("upgrade", "head")
    try:
        inspector = inspect(engine)
        assert "jobs" in inspector.get_table_names()
        columns = {col["name"] for col in inspector.get_columns("jobs")}
        assert EXPECTED_COLUMNS.issubset(columns)

        _clear_optimization_runs(engine)
        _clear_ingestion_runs(engine)
        _run_alembic("downgrade", "base")
        inspector = inspect(engine)
        assert "jobs" not in inspector.get_table_names()
    finally:
        _run_alembic("upgrade", "head")


def test_upgrade_creates_milestone_1_tables_with_expected_columns(require_database):
    """Covers every table added in Milestone 1 (rules versioning + data ingestion)."""
    from packages.database.session import engine

    _run_alembic("upgrade", "head")
    try:
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        for table_name, expected_columns in EXPECTED_TABLE_COLUMNS.items():
            assert table_name in table_names, f"expected table {table_name!r} to exist after upgrade head"
            columns = {col["name"] for col in inspector.get_columns(table_name)}
            assert expected_columns.issubset(columns), (
                f"{table_name} missing columns: {expected_columns - columns}"
            )

        _clear_optimization_runs(engine)
        _clear_ingestion_runs(engine)
        _run_alembic("downgrade", "base")
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        for table_name in EXPECTED_TABLE_COLUMNS:
            assert table_name not in table_names, (
                f"expected table {table_name!r} to be gone after downgrade base"
            )
    finally:
        _run_alembic("upgrade", "head")
