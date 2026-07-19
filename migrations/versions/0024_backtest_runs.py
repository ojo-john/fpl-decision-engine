"""backtest runs table

Milestone 7 Layer 6 (Recursive Learning Loop: BACKTEST job + the
GAMEWEEK_REVIEW -> MODEL_TRAINING retraining-trigger wiring)'s first new
table: one insert-only row per BACKTEST job run -- walk-forward prediction-
accuracy backtesting of one already-trained ``model_versions`` row, without
refitting it, against whichever historical seasons are currently usable. See
``packages/database/models/backtest_run.py`` for the full column docstrings
and design rationale (in particular why this is a dedicated table rather than
reusing ``model_metrics``).

Revision ID: 0024
Revises: 0023
Create Date: 2026-07-19

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

from packages.domain.learning import BacktestRunStatus

# revision identifiers, used by Alembic.
revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "backtest_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("job_id", UUID(as_uuid=True), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column("model_version_id", UUID(as_uuid=True), sa.ForeignKey("model_versions.id"), nullable=False),
        sa.Column("feature_version", sa.Integer(), nullable=False),
        sa.Column(
            "run_status",
            sa.Enum(BacktestRunStatus, name="backtest_run_status", native_enum=False, validate_strings=True),
            nullable=False,
        ),
        sa.Column("usable_seasons_json", JSONB(), nullable=False),
        sa.Column("fold_count", sa.Integer(), nullable=False),
        sa.Column("degenerate_single_season", sa.Boolean(), nullable=False),
        sa.Column("fold_metrics_json", JSONB(), nullable=False),
        sa.Column("aggregate_mae", sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column("aggregate_rmse", sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column("aggregate_mean_bias", sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column("aggregate_spearman", sa.Numeric(precision=6, scale=4), nullable=True),
        sa.Column("aggregate_top_20_capture_rate", sa.Numeric(precision=6, scale=4), nullable=True),
        sa.Column("n_observations", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_backtest_runs_model_version_id", "backtest_runs", ["model_version_id"])
    op.create_index("ix_backtest_runs_job_id", "backtest_runs", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_backtest_runs_job_id", table_name="backtest_runs")
    op.drop_index("ix_backtest_runs_model_version_id", table_name="backtest_runs")
    op.drop_table("backtest_runs")
