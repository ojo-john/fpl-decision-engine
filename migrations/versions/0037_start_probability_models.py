"""start_probability_models table

A SEPARATE, additive auxiliary classifier (P(player starts this gameweek)), never a change to the
main points-regression champion (model_versions/MODEL_TRAINING). See
app/jobs/handlers/start_probability_training.py's own module docstring for the full "why an
auxiliary model, not a retrain" rationale, and packages/database/models/start_probability_model.py
for why is_active defaults to False and is never flipped automatically.

Renumbered at merge time from this worktree's original "0033" (a real revision-id collision with
several other concurrently-built migrations tonight, all based on 0032). Chains after 0036
(the last of the other four gap-agents' migrations already reconciled onto main) -- safe, since
this migration only ever creates a brand-new, standalone table with no FK from anything else.

Revision ID: 0037
Revises: 0036
Create Date: 2026-08-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

# revision identifiers, used by Alembic.
revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "start_probability_models",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("feature_version", sa.Integer(), nullable=False),
        sa.Column("target_variable", sa.String(length=100), nullable=False),
        sa.Column("training_seasons_json", JSONB(), nullable=False),
        sa.Column("hyperparameters_json", JSONB(), nullable=True),
        sa.Column("artifact_uri", sa.String(), nullable=True),
        sa.Column("artifact_checksum_sha256", sa.String(length=64), nullable=True),
        sa.Column("aggregate_auc", sa.Numeric(precision=6, scale=4), nullable=True),
        sa.Column("aggregate_accuracy", sa.Numeric(precision=6, scale=4), nullable=True),
        sa.Column("aggregate_f1", sa.Numeric(precision=6, scale=4), nullable=True),
        sa.Column("aggregate_log_loss", sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column("n_observations", sa.Integer(), nullable=False),
        sa.Column("fold_metrics_json", JSONB(), nullable=False),
        sa.Column("usable_seasons_json", JSONB(), nullable=False),
        sa.Column("degenerate_single_season", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("trained_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("training_job_id", UUID(as_uuid=True), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_start_probability_models_feature_version", "start_probability_models", ["feature_version"]
    )
    op.create_index("ix_start_probability_models_is_active", "start_probability_models", ["is_active"])


def downgrade() -> None:
    op.drop_index("ix_start_probability_models_is_active", table_name="start_probability_models")
    op.drop_index("ix_start_probability_models_feature_version", table_name="start_probability_models")
    op.drop_table("start_probability_models")
