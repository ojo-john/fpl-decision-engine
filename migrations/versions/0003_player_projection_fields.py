"""player projection fields

Adds the additive, nullable-by-default columns needed by Milestone 2's
projection formula (``app/optimization/projection.py``): ``points_per_game``,
``form``, ``ep_this``, ``ep_next``, ``starts``, and
``chance_of_playing_this_round``. These are already present in the raw
bootstrap-static ``elements`` payload but were not persisted as columns in
Milestone 1.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-10

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("players", sa.Column("points_per_game", sa.Numeric(5, 1), nullable=True))
    op.add_column("players", sa.Column("form", sa.Numeric(5, 1), nullable=True))
    op.add_column("players", sa.Column("ep_this", sa.Numeric(5, 1), nullable=True))
    op.add_column("players", sa.Column("ep_next", sa.Numeric(5, 1), nullable=True))
    op.add_column("players", sa.Column("starts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("players", sa.Column("chance_of_playing_this_round", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("players", "chance_of_playing_this_round")
    op.drop_column("players", "starts")
    op.drop_column("players", "ep_next")
    op.drop_column("players", "ep_this")
    op.drop_column("players", "form")
    op.drop_column("players", "points_per_game")
