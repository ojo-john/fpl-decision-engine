"""position_segmentation_validation_runs table

NOTE for whoever reconciles this with main: this worktree's own migration chain here is
self-consistent (0033 following 0032), but this worktree was branched before this session's
other concurrent agents' work landed on main -- at least 4 other agents were working in parallel
worktrees this session (a cold-start validation job, a note-effectiveness table, an xG/defensive-
contribution feature migration, and other position-specific-modeling-adjacent work), and at least
one prior round of this exact "multiple worktrees independently claim 0033" collision was already
resolved once tonight (see ARCHITECTURE.md's "Known real gaps"/migration history for the prior
resolution: 0033 kept for note_effectiveness_evaluations, 0034 for cold_start_validation_runs,
0035 for goal_involvement/defensive_contribution). This migration almost certainly needs to be
renumbered to chain after whatever the real head is by the time this lands (0036 as of this
writing, but re-verify against the actual migrations/versions/ directory on main, don't trust this
comment blindly) -- update down_revision accordingly. This table is entirely new and additive
(no existing table's columns change), so the renumber is purely mechanical, no data-migration risk.

Historical validation of whether position-specific models (one GradientBoosting model per
GKP/DEF/MID/FWD) would out-predict the existing single global model (one model, position as a
one-hot feature) -- the exact gap app.modeling.promotion's own module docstring named explicitly:
"there is no per-position actuals breakdown computed anywhere in this codebase yet." One insert-
only row per POSITION_SEGMENTATION_VALIDATION job run -- see
packages/database/models/position_segmentation_validation_run.py for the full column docstrings,
and app/modeling/position_segmentation.py for the evaluation harness itself.

Revision ID: 0033
Revises: 0032
Create Date: 2026-08-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

from packages.domain.learning import PositionSegmentationValidationRunStatus

# revision identifiers, used by Alembic.
revision: str = "0033"
down_revision: str | None = "0032"
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
