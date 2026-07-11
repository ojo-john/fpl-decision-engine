"""optimization tables

Adds ``optimization_runs`` (one row per SQUAD_OPTIMIZATION/LINEUP_OPTIMIZATION
job run) and ``optimization_run_players`` (one row per player within that
run's recommendation) for Milestone 2's baseline optimizer.

Revision ID: 0004
Revises: 0003
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

from packages.domain.optimization import (  # noqa: E402
    OptimizationRunStatus,
    RecommendationType,
    SolverStatus,
    TargetGameweekStatus,
)
from packages.domain.rules import RulesStatus  # noqa: E402

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "optimization_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("job_id", UUID(as_uuid=True), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column("season_id", UUID(as_uuid=True), sa.ForeignKey("seasons.id"), nullable=False),
        sa.Column("rules_config_id", UUID(as_uuid=True), sa.ForeignKey("rules_config.id"), nullable=False),
        sa.Column(
            "recommendation_type",
            sa.Enum(
                RecommendationType,
                name="optimization_run_recommendation_type",
                native_enum=False,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "source_optimization_run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("optimization_runs.id"),
            nullable=True,
        ),
        sa.Column(
            "run_status",
            sa.Enum(
                OptimizationRunStatus,
                name="optimization_run_status",
                native_enum=False,
                validate_strings=True,
            ),
            nullable=False,
            server_default=OptimizationRunStatus.RUNNING.value,
        ),
        sa.Column(
            "solver_status",
            sa.Enum(
                SolverStatus,
                name="optimization_run_solver_status",
                native_enum=False,
                validate_strings=True,
            ),
            nullable=True,
        ),
        sa.Column("target_gameweek_id", UUID(as_uuid=True), sa.ForeignKey("gameweeks.id"), nullable=True),
        sa.Column(
            "target_gameweek_status",
            sa.Enum(
                TargetGameweekStatus,
                name="optimization_run_target_gameweek_status",
                native_enum=False,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column("squad_player_ids", JSONB(), nullable=True),
        sa.Column("starting_xi_player_ids", JSONB(), nullable=True),
        sa.Column("bench_player_ids_ordered", JSONB(), nullable=True),
        sa.Column("captain_player_id", UUID(as_uuid=True), sa.ForeignKey("players.id"), nullable=True),
        sa.Column("vice_captain_player_id", UUID(as_uuid=True), sa.ForeignKey("players.id"), nullable=True),
        sa.Column("squad_total_cost", sa.Integer(), nullable=True),
        sa.Column("squad_projected_points", sa.Numeric(6, 2), nullable=True),
        sa.Column("xi_projected_points", sa.Numeric(6, 2), nullable=True),
        sa.Column("optimization_permitted_snapshot", sa.Boolean(), nullable=False),
        sa.Column("data_quality_score_snapshot", sa.Numeric(5, 2), nullable=True),
        sa.Column(
            "rules_status_snapshot",
            sa.Enum(
                RulesStatus,
                name="optimization_run_rules_status_snapshot",
                native_enum=False,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column("projection_formula_version", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("parameters_json", JSONB(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_optimization_runs_season_recommendation_type",
        "optimization_runs",
        ["season_id", "recommendation_type"],
    )

    op.create_table(
        "optimization_run_players",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "optimization_run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("optimization_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("player_id", UUID(as_uuid=True), sa.ForeignKey("players.id"), nullable=False),
        sa.Column("is_starting", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("bench_order", sa.Integer(), nullable=True),
        sa.Column("is_captain", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_vice_captain", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("projected_points", sa.Numeric(6, 2), nullable=False),
        sa.Column("projection_source", sa.String(length=50), nullable=False),
        sa.Column("now_cost_snapshot", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "optimization_run_id", "player_id", name="uq_optimization_run_players_run_player"
        ),
    )
    op.create_index("ix_optimization_run_players_run_id", "optimization_run_players", ["optimization_run_id"])


def downgrade() -> None:
    op.drop_index("ix_optimization_run_players_run_id", table_name="optimization_run_players")
    op.drop_table("optimization_run_players")

    op.drop_index("ix_optimization_runs_season_recommendation_type", table_name="optimization_runs")
    op.drop_table("optimization_runs")
