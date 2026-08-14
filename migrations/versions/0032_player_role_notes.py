"""player_role_notes table

Closes a gap distinct from player_availability_risk_notes (migration 0031): who takes
penalties/corners/free-kicks is a real signal that directly affects a player's expected points,
but it is not exposed anywhere in FPL's own bootstrap-static API and is not reliably inferable
from historical scoring alone (a role can change hands mid-season, or a new signing can be handed
set-piece duty on arrival). Concrete motivating case: real Premier League Scout coverage found
this week citing Pascal Gross (Brighton) "now handling penalty duties" and "main taker of corners
and free-kicks".

Deliberately a separate table from player_availability_risk_notes rather than a reuse of it -- see
packages.database.models.player_role_note's own docstring for the full reasoning (direction:
risk notes discount, role notes boost, and a player could have both active at once and both
should apply; and multi-note tie-break direction: risk notes prefer the most severe, role notes
prefer the strongest/most specific sourced claim).

Revision ID: 0032
Revises: 0031
Create Date: 2026-08-14

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "player_role_notes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("player_id", UUID(as_uuid=True), sa.ForeignKey("players.id"), nullable=False),
        sa.Column("season_id", UUID(as_uuid=True), sa.ForeignKey("seasons.id"), nullable=False),
        sa.Column("role", sa.String(100), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("severity", sa.Numeric(4, 2), nullable=False),
        sa.Column("valid_from_fpl_event_id", sa.Integer(), nullable=True),
        sa.Column("valid_until_fpl_event_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
    )
    op.create_index("ix_player_role_notes_player_id", "player_role_notes", ["player_id"])
    op.create_index("ix_player_role_notes_season_id", "player_role_notes", ["season_id"])


def downgrade() -> None:
    op.drop_index("ix_player_role_notes_season_id", table_name="player_role_notes")
    op.drop_index("ix_player_role_notes_player_id", table_name="player_role_notes")
    op.drop_table("player_role_notes")
