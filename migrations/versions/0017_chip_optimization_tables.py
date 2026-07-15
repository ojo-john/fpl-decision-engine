"""chip optimization tables

Adds ``chip_optimization_runs`` (insert-only run-header, one row per
CHIP_SIMULATION job run), ``chip_window_candidates`` (insert-only child, one
row per evaluated candidate gameweek per chip per run), and
``chip_recommendations`` (insert-only child, one row per evaluated chip type
per run, with an optional FK to its recommended candidate) for Milestone 6
Layer 3 (Chips: job handler, persistence, REST). See
``packages/database/models/chip_optimization_run.py``,
``packages/database/models/chip_window_candidate.py``, and
``packages/database/models/chip_recommendation.py`` for the full column
docstrings and design rationale.

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-14

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

from packages.domain.chips import ChipOptimizationRunStatus, ChipType, ChipVerdict  # noqa: E402
from packages.domain.rules import RulesStatus  # noqa: E402

# revision identifiers, used by Alembic.
revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chip_optimization_runs",
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
                ChipOptimizationRunStatus,
                name="chip_optimization_run_status",
                native_enum=False,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column("target_fpl_event_id", sa.Integer(), nullable=False),
        sa.Column("projection_formula_version", sa.Integer(), nullable=False),
        sa.Column(
            "rules_status_snapshot",
            sa.Enum(
                RulesStatus,
                name="chip_optimization_run_rules_status_snapshot",
                native_enum=False,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column("optimization_permitted_snapshot", sa.Boolean(), nullable=False),
        sa.Column("data_quality_score_snapshot", sa.Numeric(5, 2), nullable=True),
        sa.Column("parameters_json", JSONB(), nullable=True),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "chip_window_candidates",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "chip_optimization_run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("chip_optimization_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "chip_type",
            sa.Enum(
                ChipType, name="chip_window_candidate_chip_type", native_enum=False, validate_strings=True
            ),
            nullable=False,
        ),
        sa.Column("window_number", sa.Integer(), nullable=False),
        sa.Column("candidate_fpl_event_id", sa.Integer(), nullable=False),
        sa.Column("is_blank_gameweek", sa.Boolean(), nullable=True),
        sa.Column("is_double_gameweek", sa.Boolean(), nullable=True),
        sa.Column("gain_mean", sa.Numeric(6, 2), nullable=False),
        sa.Column("gain_floor", sa.Numeric(6, 2), nullable=False),
        sa.Column("gain_ceiling", sa.Numeric(6, 2), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=True),
        sa.Column("rank_within_chip_type", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "chip_optimization_run_id",
            "chip_type",
            "candidate_fpl_event_id",
            name="uq_chip_window_candidates_run_chip_event",
        ),
    )
    op.create_index(
        "ix_chip_window_candidates_run_id", "chip_window_candidates", ["chip_optimization_run_id"]
    )

    op.create_table(
        "chip_recommendations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "chip_optimization_run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("chip_optimization_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "chip_type",
            sa.Enum(ChipType, name="chip_recommendation_chip_type", native_enum=False, validate_strings=True),
            nullable=False,
        ),
        sa.Column(
            "verdict",
            sa.Enum(
                ChipVerdict, name="chip_recommendation_verdict", native_enum=False, validate_strings=True
            ),
            nullable=False,
        ),
        sa.Column(
            "recommended_candidate_id",
            UUID(as_uuid=True),
            sa.ForeignKey("chip_window_candidates.id"),
            nullable=True,
        ),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "chip_optimization_run_id", "chip_type", name="uq_chip_recommendations_run_chip_type"
        ),
    )
    op.create_index("ix_chip_recommendations_run_id", "chip_recommendations", ["chip_optimization_run_id"])


def downgrade() -> None:
    op.drop_index("ix_chip_recommendations_run_id", table_name="chip_recommendations")
    op.drop_table("chip_recommendations")

    op.drop_index("ix_chip_window_candidates_run_id", table_name="chip_window_candidates")
    op.drop_table("chip_window_candidates")

    op.drop_table("chip_optimization_runs")
