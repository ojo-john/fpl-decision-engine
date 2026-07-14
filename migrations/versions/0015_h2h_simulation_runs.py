"""h2h simulation runs

Adds ``h2h_simulation_runs`` (insert-only run-header, one row per
H2H_SIMULATION job run) for Milestone 5 Layer 3 (H2H Strategy: matchup
simulation, computed by ``app.h2h.simulate.simulate_matchup`` and persisted
by ``app.jobs.handlers.h2h_simulation.H2HSimulationJobRunner``). See
``packages/database/models/h2h_simulation_run.py`` for the full column
docstrings and design rationale.

Revision ID: 0015
Revises: 0014
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

from packages.domain.h2h import H2HSimulationRunStatus, RivalStateConfidence  # noqa: E402
from packages.domain.rules import RulesStatus  # noqa: E402

# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "h2h_simulation_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("job_id", UUID(as_uuid=True), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column(
            "manager_squad_snapshot_id",
            UUID(as_uuid=True),
            sa.ForeignKey("manager_squad_snapshots.id"),
            nullable=False,
        ),
        sa.Column("rival_profile_id", UUID(as_uuid=True), sa.ForeignKey("rival_profiles.id"), nullable=False),
        sa.Column("h2h_match_fpl_id", sa.Integer(), nullable=True),
        sa.Column(
            "run_status",
            sa.Enum(
                H2HSimulationRunStatus,
                name="h2h_simulation_run_status",
                native_enum=False,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column("target_fpl_event_id", sa.Integer(), nullable=False),
        sa.Column("simulation_method_version", sa.Integer(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=True),
        sa.Column("user_score_mean", sa.Numeric(6, 2), nullable=True),
        sa.Column("user_score_floor", sa.Numeric(6, 2), nullable=True),
        sa.Column("user_score_ceiling", sa.Numeric(6, 2), nullable=True),
        sa.Column("rival_score_mean", sa.Numeric(6, 2), nullable=True),
        sa.Column("rival_score_floor", sa.Numeric(6, 2), nullable=True),
        sa.Column("rival_score_ceiling", sa.Numeric(6, 2), nullable=True),
        sa.Column("win_probability", sa.Numeric(5, 4), nullable=True),
        sa.Column("draw_probability", sa.Numeric(5, 4), nullable=True),
        sa.Column("loss_probability", sa.Numeric(5, 4), nullable=True),
        sa.Column("expected_margin", sa.Numeric(6, 2), nullable=True),
        sa.Column("margin_std_dev", sa.Numeric(6, 2), nullable=True),
        sa.Column(
            "confidence",
            sa.Enum(
                RivalStateConfidence,
                name="h2h_simulation_run_confidence",
                native_enum=False,
                validate_strings=True,
            ),
            nullable=True,
        ),
        sa.Column(
            "rules_status_snapshot",
            sa.Enum(
                RulesStatus,
                name="h2h_simulation_run_rules_status_snapshot",
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


def downgrade() -> None:
    op.drop_table("h2h_simulation_runs")
