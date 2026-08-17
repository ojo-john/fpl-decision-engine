"""player_features.goal_involvement_overperformance_5gw, defensive_contribution_5gw

Adds two new rolling feature columns, both purely additive (no existing
column's computation changes, so FEATURE_VERSION is not bumped -- matches
the precedent set by migration 0030's career_avg_early_season_points_prior_
seasons column, also additive-only):

- goal_involvement_overperformance_5gw: a regression-to-mean signal (actual
  goals+assists minus expected_goals+expected_assists over the most recent
  5 xG-covered gameweeks) -- see
  app.features.engineering.rolling_goal_involvement_overperformance.
- defensive_contribution_5gw: a rolling average of FPL's own real 2025-26+
  CBIT threshold-bonus stat -- see
  app.features.engineering.rolling_defensive_contribution.

Both columns are backfilled by re-running FEATURE_GENERATION for a season
(upserts existing feature_version rows in place); rows computed before this
migration simply have NULL here until that happens, correctly read as
"unknown", not "zero", per this table's own established convention.

Renumbered at merge time from this worktree's original "0033" (a real revision-id collision with
two other concurrently-built migrations also based on 0032: note_effectiveness_evaluations, which
kept 0033, and cold_start_validation_runs, renumbered to 0034) -- chains after both.

Revision ID: 0035
Revises: 0034
Create Date: 2026-08-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "player_features",
        sa.Column("goal_involvement_overperformance_5gw", sa.Numeric(6, 2), nullable=True),
    )
    op.add_column(
        "player_features",
        sa.Column("defensive_contribution_5gw", sa.Numeric(6, 2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("player_features", "defensive_contribution_5gw")
    op.drop_column("player_features", "goal_involvement_overperformance_5gw")
