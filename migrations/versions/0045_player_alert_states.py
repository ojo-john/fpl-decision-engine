"""player_alert_states table

Milestone 9 Layer 4 (proactive alerting). Entirely new and additive (no existing table's columns
change). One row per player, upserted in place -- see
packages/database/models/player_alert_state.py for the full column docstring on why this is a
"latest known value" table, not an insert-only audit table.

Revision ID: 0045
Revises: 0044
Create Date: 2026-08-24

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "player_alert_states",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("player_id", UUID(as_uuid=True), sa.ForeignKey("players.id"), nullable=False, unique=True),
        sa.Column("status", sa.String(length=5), nullable=True),
        sa.Column("chance_of_playing_next_round", sa.Integer(), nullable=True),
        sa.Column("price_momentum", sa.String(length=20), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("player_alert_states")
