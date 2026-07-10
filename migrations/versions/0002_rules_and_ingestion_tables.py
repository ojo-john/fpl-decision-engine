"""rules and data ingestion tables

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-10

"""

import sys
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

# Some alembic commands (e.g. `alembic history`) load version scripts
# without first running env.py (which normally inserts the repo root onto
# sys.path) — so this file adds the same path fixup defensively, letting it
# import `packages.*` regardless of which alembic command loads it.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from packages.domain.ingestion import (  # noqa: E402
    DataQualitySeverity,
    EndpointHealth,
    IngestionRunStatus,
    IngestionRunType,
)
from packages.domain.rules import RuleConfidence, RulesStatus  # noqa: E402

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "seasons",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("label", sa.String(length=20), nullable=False, unique=True),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_seasons_is_current",
        "seasons",
        ["is_current"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )

    op.create_table(
        "ingestion_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("job_id", UUID(as_uuid=True), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column(
            "run_type",
            sa.Enum(IngestionRunType, name="ingestion_run_type", native_enum=False, validate_strings=True),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                IngestionRunStatus, name="ingestion_run_status", native_enum=False, validate_strings=True
            ),
            nullable=False,
            server_default=IngestionRunStatus.RUNNING.value,
        ),
        sa.Column(
            "overall_health",
            sa.Enum(
                EndpointHealth, name="ingestion_run_overall_health", native_enum=False, validate_strings=True
            ),
            nullable=True,
        ),
        sa.Column("data_quality_score", sa.Numeric(5, 2), nullable=True),
        sa.Column("optimization_permitted", sa.Boolean(), nullable=True),
        sa.Column("report_markdown", sa.Text(), nullable=True),
        sa.Column("summary_json", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "raw_api_snapshots",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "ingestion_run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("ingestion_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("endpoint", sa.String(length=100), nullable=False),
        sa.Column("request_url", sa.String(length=500), nullable=False),
        sa.Column("request_params_json", JSONB(), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column(
            "health_status",
            sa.Enum(
                EndpointHealth,
                name="raw_api_snapshot_health_status",
                native_enum=False,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=True),
        sa.Column("schema_signature", sa.String(length=64), nullable=True),
        sa.Column("schema_drift_detected", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("raw_body", JSONB(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_raw_api_snapshots_endpoint_retrieved_at", "raw_api_snapshots", ["endpoint", "retrieved_at"]
    )

    op.create_table(
        "positions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("fpl_element_type_id", sa.Integer(), nullable=False),
        sa.Column("season_id", UUID(as_uuid=True), sa.ForeignKey("seasons.id"), nullable=False),
        sa.Column("singular_name", sa.String(length=50), nullable=False),
        sa.Column("short_name", sa.String(length=10), nullable=False),
        sa.Column("squad_select", sa.Integer(), nullable=False),
        sa.Column("squad_min_play", sa.Integer(), nullable=False),
        sa.Column("squad_max_play", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "season_id", "fpl_element_type_id", name="uq_positions_season_fpl_element_type_id"
        ),
    )

    op.create_table(
        "teams",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("fpl_team_id", sa.Integer(), nullable=False),
        sa.Column("season_id", UUID(as_uuid=True), sa.ForeignKey("seasons.id"), nullable=False),
        sa.Column("code", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("short_name", sa.String(length=10), nullable=False),
        sa.Column("strength", sa.Integer(), nullable=True),
        sa.Column("strength_overall_home", sa.Integer(), nullable=True),
        sa.Column("strength_overall_away", sa.Integer(), nullable=True),
        sa.Column("strength_attack_home", sa.Integer(), nullable=True),
        sa.Column("strength_attack_away", sa.Integer(), nullable=True),
        sa.Column("strength_defence_home", sa.Integer(), nullable=True),
        sa.Column("strength_defence_away", sa.Integer(), nullable=True),
        sa.Column("played", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("win", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("draw", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("loss", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("points", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("position", sa.Integer(), nullable=True),
        sa.Column("unavailable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("season_id", "fpl_team_id", name="uq_teams_season_fpl_team_id"),
    )

    op.create_table(
        "players",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("fpl_element_id", sa.Integer(), nullable=False),
        sa.Column("fpl_code", sa.Integer(), nullable=True),
        sa.Column("season_id", UUID(as_uuid=True), sa.ForeignKey("seasons.id"), nullable=False),
        sa.Column("team_id", UUID(as_uuid=True), sa.ForeignKey("teams.id"), nullable=True),
        sa.Column("position_id", UUID(as_uuid=True), sa.ForeignKey("positions.id"), nullable=True),
        sa.Column("first_name", sa.String(length=100), nullable=True),
        sa.Column("second_name", sa.String(length=100), nullable=True),
        sa.Column("web_name", sa.String(length=100), nullable=True),
        sa.Column("now_cost", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=5), nullable=True),
        sa.Column("total_points", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("goals_scored", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("assists", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("clean_sheets", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("goals_conceded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("own_goals", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("penalties_saved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("penalties_missed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("yellow_cards", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("red_cards", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("saves", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bonus", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bps", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("selected_by_percent", sa.Numeric(5, 1), nullable=True),
        sa.Column("chance_of_playing_next_round", sa.Integer(), nullable=True),
        sa.Column("news", sa.Text(), nullable=True),
        sa.Column("is_quarantined", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("quarantine_reason", sa.Text(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("season_id", "fpl_element_id", name="uq_players_season_fpl_element_id"),
    )
    op.create_index("ix_players_fpl_code", "players", ["fpl_code"])
    op.create_index("ix_players_team_id", "players", ["team_id"])
    op.create_index("ix_players_position_id", "players", ["position_id"])

    op.create_table(
        "gameweeks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("fpl_event_id", sa.Integer(), nullable=False),
        sa.Column("season_id", UUID(as_uuid=True), sa.ForeignKey("seasons.id"), nullable=False),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("deadline_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("data_checked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_previous", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_next", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("average_entry_score", sa.Integer(), nullable=True),
        sa.Column("highest_score", sa.Integer(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("season_id", "fpl_event_id", name="uq_gameweeks_season_fpl_event_id"),
    )
    op.create_index("ix_gameweeks_deadline_time", "gameweeks", ["deadline_time"])
    op.create_index(
        "ix_gameweeks_season_is_current",
        "gameweeks",
        ["season_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )

    op.create_table(
        "fixtures",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("fpl_fixture_id", sa.Integer(), nullable=False),
        sa.Column("season_id", UUID(as_uuid=True), sa.ForeignKey("seasons.id"), nullable=False),
        sa.Column("gameweek_id", UUID(as_uuid=True), sa.ForeignKey("gameweeks.id"), nullable=True),
        sa.Column("home_team_id", UUID(as_uuid=True), sa.ForeignKey("teams.id"), nullable=True),
        sa.Column("away_team_id", UUID(as_uuid=True), sa.ForeignKey("teams.id"), nullable=True),
        sa.Column("kickoff_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("started", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("home_score", sa.Integer(), nullable=True),
        sa.Column("away_score", sa.Integer(), nullable=True),
        sa.Column("home_difficulty", sa.Integer(), nullable=True),
        sa.Column("away_difficulty", sa.Integer(), nullable=True),
        sa.Column("is_quarantined", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("quarantine_reason", sa.Text(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("season_id", "fpl_fixture_id", name="uq_fixtures_season_fpl_fixture_id"),
    )
    op.create_index("ix_fixtures_gameweek_id", "fixtures", ["gameweek_id"])
    op.create_index("ix_fixtures_home_team_id", "fixtures", ["home_team_id"])
    op.create_index("ix_fixtures_away_team_id", "fixtures", ["away_team_id"])
    op.create_index("ix_fixtures_kickoff_time", "fixtures", ["kickoff_time"])

    op.create_table(
        "rules_config",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("season_id", UUID(as_uuid=True), sa.ForeignKey("seasons.id"), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column(
            "source_ingestion_run_id", UUID(as_uuid=True), sa.ForeignKey("ingestion_runs.id"), nullable=True
        ),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=False),
        sa.Column("raw_rules_json", JSONB(), nullable=False),
        sa.Column("raw_scoring_json", JSONB(), nullable=False),
        sa.Column("raw_chips_json", JSONB(), nullable=False),
        sa.Column("raw_element_types_json", JSONB(), nullable=False),
        sa.Column(
            "rules_status",
            sa.Enum(RulesStatus, name="rules_config_status", native_enum=False, validate_strings=True),
            nullable=False,
        ),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("change_summary_markdown", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("season_id", "version_number", name="uq_rules_config_season_version"),
    )
    op.create_index(
        "ix_rules_config_season_is_current",
        "rules_config",
        ["season_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )

    op.create_table(
        "rules_config_fields",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "rules_config_id",
            UUID(as_uuid=True),
            sa.ForeignKey("rules_config.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("field_path", sa.String(length=255), nullable=False),
        sa.Column("value_json", JSONB(), nullable=True),
        sa.Column(
            "confidence",
            sa.Enum(
                RuleConfidence, name="rules_config_field_confidence", native_enum=False, validate_strings=True
            ),
            nullable=False,
        ),
        sa.Column("source", sa.String(length=100), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("rules_config_id", "field_path", name="uq_rules_config_fields_config_path"),
    )
    op.create_index("ix_rules_config_fields_field_path", "rules_config_fields", ["field_path"])

    op.create_table(
        "data_quality_issues",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "ingestion_run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("ingestion_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("entity_type", sa.String(length=50), nullable=False),
        sa.Column("entity_fpl_id", sa.Integer(), nullable=True),
        sa.Column(
            "severity",
            sa.Enum(
                DataQualitySeverity,
                name="data_quality_issue_severity",
                native_enum=False,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column("check_name", sa.String(length=100), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_data_quality_issues_run_severity", "data_quality_issues", ["ingestion_run_id", "severity"]
    )


def downgrade() -> None:
    op.drop_index("ix_data_quality_issues_run_severity", table_name="data_quality_issues")
    op.drop_table("data_quality_issues")

    op.drop_index("ix_rules_config_fields_field_path", table_name="rules_config_fields")
    op.drop_table("rules_config_fields")

    op.drop_index("ix_rules_config_season_is_current", table_name="rules_config")
    op.drop_table("rules_config")

    op.drop_index("ix_fixtures_kickoff_time", table_name="fixtures")
    op.drop_index("ix_fixtures_away_team_id", table_name="fixtures")
    op.drop_index("ix_fixtures_home_team_id", table_name="fixtures")
    op.drop_index("ix_fixtures_gameweek_id", table_name="fixtures")
    op.drop_table("fixtures")

    op.drop_index("ix_gameweeks_season_is_current", table_name="gameweeks")
    op.drop_index("ix_gameweeks_deadline_time", table_name="gameweeks")
    op.drop_table("gameweeks")

    op.drop_index("ix_players_position_id", table_name="players")
    op.drop_index("ix_players_team_id", table_name="players")
    op.drop_index("ix_players_fpl_code", table_name="players")
    op.drop_table("players")

    op.drop_table("teams")

    op.drop_table("positions")

    op.drop_index("ix_raw_api_snapshots_endpoint_retrieved_at", table_name="raw_api_snapshots")
    op.drop_table("raw_api_snapshots")

    op.drop_table("ingestion_runs")

    op.drop_index("ix_seasons_is_current", table_name="seasons")
    op.drop_table("seasons")
