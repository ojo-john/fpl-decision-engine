"""player_price_signal_observations and price_signal_calibrations tables

Closes the gap that made app.players.price_signal's thresholds untunable:
there was previously no historical record of what the transfer-momentum
signal looked like at any past point in time (players.transfers_in_event/
transfers_out_event/selected_by_percent are overwritten every DATA_INGESTION
run), so a real price change observed in player_price_history could never be
paired back against "what did the signal say right before this happened".

player_price_signal_observations is an insert-only log, one row per
current-season player per DATA_INGESTION run (see
app.ingestion.normalization.record_price_signal_observations).

price_signal_calibrations holds the history of auto-tuned thresholds, one
active row at a time (same is_current/is_champion/is_priority invariant used
elsewhere in this schema), populated by
app.jobs.handlers.gameweek_review.GameweekReviewJobRunner
._maybe_recalibrate_price_signal once enough real observations exist.

Revision ID: 0029
Revises: 0028
Create Date: 2026-07-24

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "player_price_signal_observations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("player_id", UUID(as_uuid=True), sa.ForeignKey("players.id"), nullable=False),
        sa.Column("season_id", UUID(as_uuid=True), sa.ForeignKey("seasons.id"), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("transfers_in_event", sa.Integer(), nullable=True),
        sa.Column("transfers_out_event", sa.Integer(), nullable=True),
        sa.Column("selected_by_percent", sa.Numeric(5, 1), nullable=True),
        sa.Column("total_players", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_player_price_signal_observations_player_id_observed_at",
        "player_price_signal_observations",
        ["player_id", "observed_at"],
    )
    op.create_index(
        "ix_player_price_signal_observations_season_id",
        "player_price_signal_observations",
        ["season_id"],
    )

    op.create_table(
        "price_signal_calibrations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("ratio_threshold", sa.Numeric(6, 4), nullable=False),
        sa.Column("absolute_floor", sa.Integer(), nullable=False),
        sa.Column("sample_size", sa.Integer(), nullable=False),
        sa.Column("accuracy", sa.Numeric(5, 4), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("calibrated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("price_signal_calibrations")
    op.drop_index(
        "ix_player_price_signal_observations_season_id", table_name="player_price_signal_observations"
    )
    op.drop_index(
        "ix_player_price_signal_observations_player_id_observed_at",
        table_name="player_price_signal_observations",
    )
    op.drop_table("player_price_signal_observations")
