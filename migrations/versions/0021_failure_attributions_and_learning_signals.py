"""failure attributions and learning signals tables

Adds ``failure_attributions`` and ``learning_signals`` -- Milestone 7 Layer 3
(Recursive Learning Loop: Failure Attribution/Rolling Learning Signals)'s two
new tables. ``failure_attributions`` records zero or more mechanically-
classified causes per ``recommendation_outcomes`` row;
``learning_signals`` records one insert-only row per rolling-window
computation (projection MAE, H2H calibration, chip value, failure-reason
concentration). See ``packages/database/models/failure_attribution.py`` and
``packages/database/models/learning_signal.py`` for the full column
docstrings and design rationale.

Revision ID: 0021
Revises: 0020
Create Date: 2026-07-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

from packages.domain.learning import FailureCategory, FailureReasonCode, LearningSignalType

# revision identifiers, used by Alembic.
revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "failure_attributions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "recommendation_outcome_id",
            UUID(as_uuid=True),
            sa.ForeignKey("recommendation_outcomes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "reason_code",
            sa.Enum(
                FailureReasonCode,
                name="failure_attribution_reason_code",
                native_enum=False,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "category",
            sa.Enum(
                FailureCategory,
                name="failure_attribution_category",
                native_enum=False,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column("severity", sa.Numeric(precision=6, scale=2), nullable=True),
        sa.Column("evidence_json", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_failure_attributions_outcome_id", "failure_attributions", ["recommendation_outcome_id"]
    )

    op.create_table(
        "learning_signals",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("season_id", UUID(as_uuid=True), sa.ForeignKey("seasons.id"), nullable=False),
        sa.Column("as_of_gameweek_id", UUID(as_uuid=True), sa.ForeignKey("gameweeks.id"), nullable=False),
        sa.Column(
            "signal_type",
            sa.Enum(
                LearningSignalType,
                name="learning_signal_signal_type",
                native_enum=False,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column("window_size_gameweeks", sa.Integer(), nullable=False),
        sa.Column("value", sa.Numeric(precision=10, scale=4), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("threshold_breached", sa.Boolean(), nullable=False),
        sa.Column("threshold_config_snapshot_json", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_learning_signals_season_signal_type", "learning_signals", ["season_id", "signal_type"]
    )
    op.create_index("ix_learning_signals_as_of_gameweek_id", "learning_signals", ["as_of_gameweek_id"])


def downgrade() -> None:
    op.drop_index("ix_learning_signals_as_of_gameweek_id", table_name="learning_signals")
    op.drop_index("ix_learning_signals_season_signal_type", table_name="learning_signals")
    op.drop_table("learning_signals")
    op.drop_index("ix_failure_attributions_outcome_id", table_name="failure_attributions")
    op.drop_table("failure_attributions")
