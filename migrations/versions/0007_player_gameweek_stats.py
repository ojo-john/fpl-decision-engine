"""player gameweek stats table

Adds ``player_gameweek_stats`` — one row per ``(player_id, gameweek_id)`` —
for Milestone 3 Layer 1's historical data ingestion (per-gameweek stat lines
sourced from vaastav/Fantasy-Premier-League's ``gws/merged_gw.csv``; see
``app/ingestion/historical_normalization.py`` and
``packages/database/models/player_gameweek_stats.py`` for the full
docstring/attribution note).

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-11

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "player_gameweek_stats",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("player_id", UUID(as_uuid=True), sa.ForeignKey("players.id"), nullable=False),
        sa.Column("season_id", UUID(as_uuid=True), sa.ForeignKey("seasons.id"), nullable=False),
        sa.Column("gameweek_id", UUID(as_uuid=True), sa.ForeignKey("gameweeks.id"), nullable=False),
        sa.Column("fixture_id", UUID(as_uuid=True), sa.ForeignKey("fixtures.id"), nullable=True),
        sa.Column("opponent_team_id", UUID(as_uuid=True), sa.ForeignKey("teams.id"), nullable=True),
        sa.Column("was_home", sa.Boolean(), nullable=True),
        sa.Column("position_short_name_at_time", sa.String(length=10), nullable=True),
        sa.Column("minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_points", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("goals_scored", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("assists", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("clean_sheets", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("goals_conceded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("own_goals", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("penalties_saved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("penalties_missed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("yellow_cards", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("red_cards", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("saves", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bonus", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bps", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ict_index", sa.Numeric(6, 1), nullable=True),
        sa.Column("influence", sa.Numeric(6, 1), nullable=True),
        sa.Column("creativity", sa.Numeric(6, 1), nullable=True),
        sa.Column("threat", sa.Numeric(6, 1), nullable=True),
        sa.Column("expected_goals", sa.Numeric(5, 2), nullable=True),
        sa.Column("expected_assists", sa.Numeric(5, 2), nullable=True),
        sa.Column("expected_goal_involvements", sa.Numeric(5, 2), nullable=True),
        sa.Column("expected_goals_conceded", sa.Numeric(5, 2), nullable=True),
        sa.Column("starts", sa.Boolean(), nullable=True),
        sa.Column("defensive_contribution", sa.Integer(), nullable=True),
        sa.Column("clearances_blocks_interceptions", sa.Integer(), nullable=True),
        sa.Column("recoveries", sa.Integer(), nullable=True),
        sa.Column("tackles", sa.Integer(), nullable=True),
        sa.Column("xp_estimate", sa.Numeric(5, 2), nullable=True),
        sa.Column("value_at_time", sa.Integer(), nullable=True),
        sa.Column("selected_by_count_at_time", sa.Integer(), nullable=True),
        sa.Column("transfers_in", sa.Integer(), nullable=True),
        sa.Column("transfers_out", sa.Integer(), nullable=True),
        sa.Column("transfers_balance", sa.Integer(), nullable=True),
        sa.Column("data_source", sa.String(length=50), nullable=False),
        sa.Column("extra_stats_json", JSONB(), nullable=True),
        sa.Column("is_quarantined", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("quarantine_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("player_id", "gameweek_id", name="uq_player_gameweek_stats_player_gameweek"),
    )
    op.create_index("ix_player_gameweek_stats_season_id", "player_gameweek_stats", ["season_id"])
    op.create_index("ix_player_gameweek_stats_gameweek_id", "player_gameweek_stats", ["gameweek_id"])


def downgrade() -> None:
    op.drop_index("ix_player_gameweek_stats_gameweek_id", table_name="player_gameweek_stats")
    op.drop_index("ix_player_gameweek_stats_season_id", table_name="player_gameweek_stats")
    op.drop_table("player_gameweek_stats")
