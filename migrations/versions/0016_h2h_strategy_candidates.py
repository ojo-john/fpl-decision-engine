"""h2h strategy candidates

Adds ``h2h_strategy_candidates`` (insert-only, one row per named strategy
evaluated for a ``h2h_simulation_runs`` row — Milestone 5 Layer 4: H2H
Strategy risk-adjusted strategy comparison), then ``ALTER TABLE``s
``h2h_simulation_runs`` to add ``recommended_candidate_id`` (a FK to the new
table, so it can only be added after the table exists) and
``h2h_adjustment_materially_changed``. See
``packages/database/models/h2h_strategy_candidate.py`` and
``packages/database/models/h2h_simulation_run.py`` for the full column
docstrings and design rationale.

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-14

"""

import sys
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# Some alembic commands (e.g. `alembic history`) load version scripts
# without first running env.py (which normally inserts the repo root onto
# sys.path) — so this file adds the same path fixup defensively, letting it
# import `packages.*` regardless of which alembic command loads it.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from packages.domain.h2h import H2HStrategyLabel  # noqa: E402

# revision identifiers, used by Alembic.
revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "h2h_strategy_candidates",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "h2h_simulation_run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("h2h_simulation_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "strategy_label",
            sa.Enum(
                H2HStrategyLabel,
                name="h2h_strategy_candidate_label",
                native_enum=False,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "transfer_optimization_run_scenario_id",
            UUID(as_uuid=True),
            sa.ForeignKey("transfer_optimization_run_scenarios.id"),
            nullable=True,
        ),
        sa.Column("captain_player_id", UUID(as_uuid=True), sa.ForeignKey("players.id"), nullable=True),
        sa.Column("win_probability", sa.Numeric(5, 4), nullable=False),
        sa.Column("draw_probability", sa.Numeric(5, 4), nullable=False),
        sa.Column("loss_probability", sa.Numeric(5, 4), nullable=False),
        sa.Column("expected_margin", sa.Numeric(6, 2), nullable=False),
        sa.Column("is_recommended", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "h2h_simulation_run_id", "strategy_label", name="uq_h2h_strategy_candidates_run_label"
        ),
    )
    op.create_index("ix_h2h_strategy_candidates_run_id", "h2h_strategy_candidates", ["h2h_simulation_run_id"])

    op.add_column(
        "h2h_simulation_runs",
        sa.Column(
            "recommended_candidate_id",
            UUID(as_uuid=True),
            sa.ForeignKey("h2h_strategy_candidates.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "h2h_simulation_runs",
        sa.Column("h2h_adjustment_materially_changed", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("h2h_simulation_runs", "h2h_adjustment_materially_changed")
    op.drop_column("h2h_simulation_runs", "recommended_candidate_id")

    op.drop_index("ix_h2h_strategy_candidates_run_id", table_name="h2h_strategy_candidates")
    op.drop_table("h2h_strategy_candidates")
