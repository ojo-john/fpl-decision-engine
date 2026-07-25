"""player_availability_risk_notes table

Closes a gap FPL's own official injury/doubt data doesn't cover: a player who
isn't officially listed as injured/doubtful but has a real, sourced reason to
expect reduced early-season involvement. FPL's own signal
(players.chance_of_playing_next_round/news) already refreshes automatically
every DATA_INGESTION run and app.optimization.projection.project_player
already down-weights against it -- no new table needed for that part.

Concrete motivating case (added the same week this migration was written):
several Premier League players played deep into the 2026 FIFA World Cup
(final July 19, 2026), with the 2026-27 Premier League season kicking off
just 33 days later (Aug 21). Two Arsenal players are confirmed high-risk by
real reporting (Declan Rice, Bukayo Saka -- both not returning to training
until Aug 10, only 11 days before kickoff) despite neither being officially
flagged injured/doubtful by FPL at time of writing.

Each row is an independent, curated note -- not the single-active-row
invariant used elsewhere in this schema (seasons.is_current,
model_versions.is_champion, leagues.is_priority) -- see
packages.database.models.player_availability_risk_note's own docstring.

Revision ID: 0031
Revises: 0030
Create Date: 2026-07-25

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "player_availability_risk_notes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("player_id", UUID(as_uuid=True), sa.ForeignKey("players.id"), nullable=False),
        sa.Column("season_id", UUID(as_uuid=True), sa.ForeignKey("seasons.id"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("severity", sa.Numeric(3, 2), nullable=False),
        sa.Column("valid_from_fpl_event_id", sa.Integer(), nullable=True),
        sa.Column("valid_until_fpl_event_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
    )
    op.create_index(
        "ix_player_availability_risk_notes_player_id",
        "player_availability_risk_notes",
        ["player_id"],
    )
    op.create_index(
        "ix_player_availability_risk_notes_season_id",
        "player_availability_risk_notes",
        ["season_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_player_availability_risk_notes_season_id", table_name="player_availability_risk_notes"
    )
    op.drop_index(
        "ix_player_availability_risk_notes_player_id", table_name="player_availability_risk_notes"
    )
    op.drop_table("player_availability_risk_notes")
