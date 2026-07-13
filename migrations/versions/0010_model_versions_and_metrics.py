"""model versions and metrics tables

Adds ``model_versions`` and ``model_metrics`` — Milestone 3 Layer 3's
trained/evaluated candidate rows and their walk-forward validation metrics.
See ``packages/database/models/model_version.py``/``model_metric.py`` for
the full column docstrings, and ``app/jobs/handlers/model_training.py`` for
the ``MODEL_TRAINING`` job that populates them.

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-12

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

from packages.domain.modeling import ModelFamily

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_versions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("model_name", sa.String(), nullable=False),
        sa.Column(
            "model_family",
            sa.Enum(ModelFamily, name="model_version_model_family", native_enum=False, validate_strings=True),
            nullable=False,
        ),
        sa.Column("feature_version", sa.Integer(), nullable=False),
        sa.Column("target_variable", sa.String(length=50), nullable=False),
        sa.Column("training_seasons_json", JSONB(), nullable=False),
        sa.Column("hyperparameters_json", JSONB(), nullable=True),
        sa.Column("artifact_uri", sa.String(), nullable=True),
        sa.Column("artifact_checksum_sha256", sa.String(length=64), nullable=True),
        sa.Column("is_champion", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("trained_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("training_job_id", UUID(as_uuid=True), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_model_versions_feature_version", "model_versions", ["feature_version"])
    op.create_index("ix_model_versions_is_champion", "model_versions", ["is_champion"])

    op.create_table(
        "model_metrics",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("model_version_id", UUID(as_uuid=True), sa.ForeignKey("model_versions.id"), nullable=False),
        sa.Column("fold_index", sa.Integer(), nullable=True),
        sa.Column("validation_season_label", sa.String(length=20), nullable=True),
        sa.Column("mae", sa.Numeric(8, 4), nullable=False),
        sa.Column("rmse", sa.Numeric(8, 4), nullable=False),
        sa.Column("mean_bias", sa.Numeric(8, 4), nullable=False),
        sa.Column("spearman_rank_corr", sa.Numeric(6, 4), nullable=True),
        sa.Column("top_20_capture_rate", sa.Numeric(6, 4), nullable=True),
        sa.Column("calibration_bucket_json", JSONB(), nullable=True),
        sa.Column("n_observations", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "model_version_id", "fold_index", name="uq_model_metrics_model_version_fold_index"
        ),
    )
    op.create_index("ix_model_metrics_model_version_id", "model_metrics", ["model_version_id"])


def downgrade() -> None:
    op.drop_index("ix_model_metrics_model_version_id", table_name="model_metrics")
    op.drop_table("model_metrics")
    op.drop_index("ix_model_versions_is_champion", table_name="model_versions")
    op.drop_index("ix_model_versions_feature_version", table_name="model_versions")
    op.drop_table("model_versions")
