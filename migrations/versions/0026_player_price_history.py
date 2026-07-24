"""player price history table

Insert-only: one row per OBSERVED players.now_cost change, appended by
app.ingestion.normalization.upsert_players only when a DATA_INGESTION run's
incoming now_cost differs from what was already stored -- never once per
ingestion run. See packages/database/models/player_price_history.py for the
full column docstrings and design rationale.

Revision ID: 0026
Revises: 0025
Create Date: 2026-07-24

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "player_price_history",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("player_id", UUID(as_uuid=True), sa.ForeignKey("players.id"), nullable=False),
        sa.Column("season_id", UUID(as_uuid=True), sa.ForeignKey("seasons.id"), nullable=False),
        sa.Column("old_cost", sa.Integer(), nullable=False),
        sa.Column("new_cost", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_player_price_history_player_id", "player_price_history", ["player_id"])
    op.create_index("ix_player_price_history_season_id", "player_price_history", ["season_id"])


def downgrade() -> None:
    op.drop_index("ix_player_price_history_season_id", table_name="player_price_history")
    op.drop_index("ix_player_price_history_player_id", table_name="player_price_history")
    op.drop_table("player_price_history")
