"""position_segmentation_validation_runs table

Renumbered at merge time from this worktree's original "0033" (the second round of the same
revision-id collision tonight -- 5 agents worked in parallel worktrees; 0033-0037 were already
claimed by note_effectiveness_evaluations, cold_start_validation_runs, goal_involvement/
defensive_contribution, gameweek_schedule_gap_days, and start_probability_models respectively).
Chains after 0037. This table is entirely new and additive (no existing table's columns change),
so the renumber is purely mechanical, no data-migration risk.

Historical validation of whether position-specific models (one GradientBoosting model per
GKP/DEF/MID/FWD) would out-predict the existing single global model (one model, position as a
one-hot feature) -- the exact gap app.modeling.promotion's own module docstring named explicitly:
"there is no per-position actuals breakdown computed anywhere in this codebase yet." One insert-
only row per POSITION_SEGMENTATION_VALIDATION job run -- see
packages/database/models/position_segmentation_validation_run.py for the full column docstrings,
and app/modeling/position_segmentation.py for the evaluation harness itself.

Revision ID: 0038
Revises: 0037
Create Date: 2026-08-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

from packages.domain.learning import PositionSegmentationValidationRunStatus

# revision identifiers, used by Alembic.
revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "position_segmentation_validation_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("job_id", UUID(as_uuid=True), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column(
            "run_status",
            sa.Enum(
                PositionSegmentationValidationRunStatus,
                name="position_segmentation_validation_run_status",
                native_enum=False,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column("feature_version", sa.Integer(), nullable=False),
        sa.Column("usable_seasons_json", JSONB(), nullable=False),
        sa.Column("fold_count", sa.Integer(), nullable=False),
        sa.Column("degenerate_single_season", sa.Boolean(), nullable=False),
        sa.Column("per_position_json", JSONB(), nullable=False),
        sa.Column("overall_global_mae", sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column("overall_position_specific_mae", sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column("n_observations", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_position_segmentation_validation_runs_job_id",
        "position_segmentation_validation_runs",
        ["job_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_position_segmentation_validation_runs_job_id",
        table_name="position_segmentation_validation_runs",
    )
    op.drop_table("position_segmentation_validation_runs")
