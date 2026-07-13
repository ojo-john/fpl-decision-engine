"""transfer optimization tables

Adds ``transfer_optimization_runs`` (one row per TRANSFER_OPTIMIZATION job
run), ``transfer_optimization_run_scenarios`` (one row per evaluated 0/1/2
-transfer scenario, always exactly 3 per SUCCEEDED run), and
``transfer_optimization_run_moves`` (one row per player-out/player-in swap
within a scenario) for Milestone 4 Layer 3. See
``packages/database/models/transfer_optimization_run*.py`` for the full
column docstrings and design rationale.

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-13

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

from packages.domain.rules import RulesStatus  # noqa: E402
from packages.domain.transfers import TransferOptimizationRunStatus  # noqa: E402

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "transfer_optimization_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("job_id", UUID(as_uuid=True), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column(
            "manager_squad_snapshot_id",
            UUID(as_uuid=True),
            sa.ForeignKey("manager_squad_snapshots.id"),
            nullable=False,
        ),
        sa.Column("season_id", UUID(as_uuid=True), sa.ForeignKey("seasons.id"), nullable=False),
        sa.Column("rules_config_id", UUID(as_uuid=True), sa.ForeignKey("rules_config.id"), nullable=False),
        sa.Column(
            "run_status",
            sa.Enum(
                TransferOptimizationRunStatus,
                name="transfer_optimization_run_status",
                native_enum=False,
                validate_strings=True,
            ),
            nullable=False,
            server_default=TransferOptimizationRunStatus.RUNNING.value,
        ),
        sa.Column(
            "horizon_start_gameweek_id", UUID(as_uuid=True), sa.ForeignKey("gameweeks.id"), nullable=True
        ),
        sa.Column("horizon_length", sa.Integer(), nullable=False),
        sa.Column("horizon_gameweek_ids_json", JSONB(), nullable=True),
        sa.Column("free_transfers_available_snapshot", sa.Integer(), nullable=False),
        sa.Column("bank_snapshot", sa.Integer(), nullable=False),
        sa.Column("transfer_hit_cost_snapshot", sa.Integer(), nullable=False),
        sa.Column("recommended_transfers_count", sa.Integer(), nullable=True),
        sa.Column("recommended_hit_cost", sa.Integer(), nullable=True),
        sa.Column("recommended_net_horizon_points_gain", sa.Numeric(6, 2), nullable=True),
        sa.Column("optimization_permitted_snapshot", sa.Boolean(), nullable=False),
        sa.Column("data_quality_score_snapshot", sa.Numeric(5, 2), nullable=True),
        sa.Column(
            "rules_status_snapshot",
            sa.Enum(
                RulesStatus,
                name="transfer_optimization_run_rules_status_snapshot",
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

    op.create_table(
        "transfer_optimization_run_scenarios",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "transfer_optimization_run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("transfer_optimization_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("transfers_count", sa.Integer(), nullable=False),
        sa.Column("raw_horizon_points_gain", sa.Numeric(6, 2), nullable=False),
        sa.Column("hit_cost", sa.Integer(), nullable=False),
        sa.Column("net_horizon_points_gain", sa.Numeric(6, 2), nullable=False),
        sa.Column("is_recommended", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "transfer_optimization_run_id", "transfers_count", name="uq_transfer_opt_run_scenarios_run_count"
        ),
    )
    op.create_index(
        "ix_transfer_opt_run_scenarios_run_id",
        "transfer_optimization_run_scenarios",
        ["transfer_optimization_run_id"],
    )

    op.create_table(
        "transfer_optimization_run_moves",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "transfer_optimization_run_scenario_id",
            UUID(as_uuid=True),
            sa.ForeignKey("transfer_optimization_run_scenarios.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("move_index", sa.Integer(), nullable=False),
        sa.Column("player_out_id", UUID(as_uuid=True), sa.ForeignKey("players.id"), nullable=False),
        sa.Column("player_out_selling_price_snapshot", sa.Integer(), nullable=False),
        sa.Column("player_in_id", UUID(as_uuid=True), sa.ForeignKey("players.id"), nullable=False),
        sa.Column("player_in_cost_snapshot", sa.Integer(), nullable=False),
        sa.Column("projected_points_gain_over_horizon", sa.Numeric(6, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_transfer_opt_run_moves_scenario_id",
        "transfer_optimization_run_moves",
        ["transfer_optimization_run_scenario_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_transfer_opt_run_moves_scenario_id", table_name="transfer_optimization_run_moves")
    op.drop_table("transfer_optimization_run_moves")

    op.drop_index("ix_transfer_opt_run_scenarios_run_id", table_name="transfer_optimization_run_scenarios")
    op.drop_table("transfer_optimization_run_scenarios")

    op.drop_table("transfer_optimization_runs")
