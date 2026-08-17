"""teams.elo_rating, teams.elo_last_synced_at, player_features.next_fixture_elo_differential

Investigates a real gap: FPL's own next_fixture_difficulty is a coarse, subjective, integer 1-5
scale set once per fixture by FPL's own editorial team, not a continuously-updated measure of
real team strength. ClubElo.com (http://api.clubelo.com, confirmed live and free -- no API key,
no documented rate limit beyond "be reasonable") publishes a real, continuously-updated Elo
rating per club, derived purely from real match results. See app.ingestion.team_strength for the
sync job that populates teams.elo_rating.

next_fixture_elo_differential (this player's team's Elo minus their next opponent's, a raw,
signed differential) is computed and persisted, but real validation
(scripts/validate_elo_fixture_adjustment.py, n=3035 real 2025-26 GW1-6 observations) found it
does NOT improve app.optimization.projection._cold_start_projection's own fixture-adjustment
formula over the existing FPL-difficulty one -- every tested scale was worse, monotonically. That
hand-written formula therefore still uses only next_fixture_difficulty, unchanged. This column is
still valuable: it reaches the TRAINED champion model's own feature matrix automatically
(app.modeling.candidates), where a gradient-boosted model can learn its own weight for it, unlike
a single fixed coefficient in a hand-written formula.

Both new player_features/teams columns are purely additive -- no existing column's computation
changes, so FEATURE_VERSION is not bumped (matches the precedent set by migration 0030's
career_avg_early_season_points_prior_seasons column).

Renumbered at merge time from this worktree's original "0033" (branched off an old commit,
before the other four gap-agents' migrations tonight -- 0033 through 0039 -- landed on main).
Purely additive (new nullable columns only), safe to chain after everything else.

Revision ID: 0040
Revises: 0039
Create Date: 2026-08-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("teams", sa.Column("elo_rating", sa.Numeric(precision=8, scale=3), nullable=True))
    op.add_column(
        "teams", sa.Column("elo_last_synced_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "player_features",
        sa.Column("next_fixture_elo_differential", sa.Numeric(precision=8, scale=3), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("player_features", "next_fixture_elo_differential")
    op.drop_column("teams", "elo_last_synced_at")
    op.drop_column("teams", "elo_rating")
