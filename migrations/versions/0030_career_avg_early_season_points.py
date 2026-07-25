"""player_features.career_avg_early_season_points_prior_seasons

Adds a cross-season "early gameweeks only" points average alongside the
existing full-season career_avg_points_prior_seasons -- a player's
season-opening form can genuinely differ from their full-season average
(well-known slow/fast starters), which the full-season average alone
papers over. See app.features.engineering.career_avg_early_season_points_
prior_seasons for the computation and app.optimization.projection._cold_
start_projection for where the cold-start blend now prefers this value.

Revision ID: 0030
Revises: 0029
Create Date: 2026-07-25

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "player_features",
        sa.Column("career_avg_early_season_points_prior_seasons", sa.Numeric(6, 2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("player_features", "career_avg_early_season_points_prior_seasons")
