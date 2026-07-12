"""player features table

Adds ``player_features`` — Milestone 3 Layer 2's engineered, leakage-safe
feature rows, one per ``(player_id, gameweek_id, feature_version)`` — where
``gameweek_id`` is the TARGET gameweek being predicted for. See
``app/features/engineering.py`` and ``packages/database/models/player_features.py``
for the full leakage-prevention rule and column docstrings.

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-12

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "player_features",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("player_id", UUID(as_uuid=True), sa.ForeignKey("players.id"), nullable=False),
        sa.Column("season_id", UUID(as_uuid=True), sa.ForeignKey("seasons.id"), nullable=False),
        sa.Column("gameweek_id", UUID(as_uuid=True), sa.ForeignKey("gameweeks.id"), nullable=False),
        sa.Column("feature_version", sa.Integer(), nullable=False),
        sa.Column("cutoff_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("target_total_points", sa.Integer(), nullable=True),
        sa.Column("rolling_avg_points_3gw", sa.Numeric(6, 2), nullable=True),
        sa.Column("rolling_avg_points_5gw", sa.Numeric(6, 2), nullable=True),
        sa.Column("rolling_avg_points_10gw", sa.Numeric(6, 2), nullable=True),
        sa.Column("rolling_avg_minutes_3gw", sa.Numeric(6, 2), nullable=True),
        sa.Column("rolling_avg_minutes_5gw", sa.Numeric(6, 2), nullable=True),
        sa.Column("season_avg_points_to_date", sa.Numeric(6, 2), nullable=True),
        sa.Column("career_avg_points_prior_seasons", sa.Numeric(6, 2), nullable=True),
        sa.Column("form_weighted_avg_points", sa.Numeric(6, 2), nullable=True),
        sa.Column("home_avg_points", sa.Numeric(6, 2), nullable=True),
        sa.Column("away_avg_points", sa.Numeric(6, 2), nullable=True),
        sa.Column("next_fixture_difficulty", sa.Integer(), nullable=True),
        sa.Column("next_fixture_was_home", sa.Boolean(), nullable=True),
        sa.Column("rest_days", sa.Integer(), nullable=True),
        sa.Column("price_at_time", sa.Numeric(6, 1), nullable=True),
        sa.Column("ownership_at_time", sa.Integer(), nullable=True),
        sa.Column("position_short_name", sa.String(length=10), nullable=True),
        sa.Column("is_missing_history", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("data_source", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "player_id",
            "gameweek_id",
            "feature_version",
            name="uq_player_features_player_gameweek_version",
        ),
    )
    op.create_index("ix_player_features_season_id", "player_features", ["season_id"])
    op.create_index("ix_player_features_gameweek_id", "player_features", ["gameweek_id"])


def downgrade() -> None:
    op.drop_index("ix_player_features_gameweek_id", table_name="player_features")
    op.drop_index("ix_player_features_season_id", table_name="player_features")
    op.drop_table("player_features")
