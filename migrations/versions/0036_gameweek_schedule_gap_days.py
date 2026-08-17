"""player_features.gameweek_schedule_gap_days

Adds one new, purely additive feature column (FEATURE_VERSION not bumped -- same precedent as
migration 0035's goal_involvement_overperformance_5gw/defensive_contribution_5gw and migration
0030's career_avg_early_season_points_prior_seasons, both also additive-only):

- gameweek_schedule_gap_days: days since the PREVIOUS gameweek's own deadline, in the same
  season -- a GAMEWEEK-level (not player-specific) structural signal, distinct from the existing
  player-specific ``rest_days`` column. See
  app.features.engineering.gameweek_schedule_gap_days's own docstring for the full rationale and
  the real, measured effect (checked against this app's own 6 real historical seasons: players
  who started the gameweek immediately before a >=12-day gap averaged 2.836 points / 69.5 minutes
  in the gameweek immediately after it, versus 3.541 points / 85.8 minutes for the general
  population of all >=60-minute appearances).

Backfilled by re-running FEATURE_GENERATION for a season (upserts existing feature_version rows
in place); rows computed before this migration simply have NULL here until that happens, read as
"unknown", not "zero", per this table's own established convention.

Revision ID: 0036
Revises: 0035
Create Date: 2026-08-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "player_features",
        sa.Column("gameweek_schedule_gap_days", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("player_features", "gameweek_schedule_gap_days")
