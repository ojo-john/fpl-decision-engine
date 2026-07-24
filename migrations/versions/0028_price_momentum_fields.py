"""players.transfers_in_event/transfers_out_event/cost_change_event, seasons.total_players

Adds the raw signal a price-rise/fall heuristic needs (app.players.price_signal):
bootstrap-static's per-player, per-gameweek transfer counts (reset to 0 by FPL at
the start of every gameweek -- distinct from player_gameweek_stats.transfers_in/
transfers_out, which are season-cumulative and sourced from a different pipeline),
plus the season-level total_players needed to turn a player's selected_by_percent
into an estimated absolute owner count.

Revision ID: 0028
Revises: 0027
Create Date: 2026-07-24

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("players", sa.Column("transfers_in_event", sa.Integer(), nullable=True))
    op.add_column("players", sa.Column("transfers_out_event", sa.Integer(), nullable=True))
    op.add_column("players", sa.Column("cost_change_event", sa.Integer(), nullable=True))
    op.add_column("seasons", sa.Column("total_players", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("seasons", "total_players")
    op.drop_column("players", "cost_change_event")
    op.drop_column("players", "transfers_out_event")
    op.drop_column("players", "transfers_in_event")
