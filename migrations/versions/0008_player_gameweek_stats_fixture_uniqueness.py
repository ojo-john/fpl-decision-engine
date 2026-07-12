"""widen player_gameweek_stats uniqueness to include fixture_id (double gameweek fix)

The original ``uq_player_gameweek_stats_player_gameweek`` constraint on
``(player_id, gameweek_id)`` cannot represent a Double Gameweek — a real,
recurring FPL mechanic where a player has two fixtures in the same
gameweek round. Verified against a real historical backfill (2024-25
season, round 24 was a Double Gameweek): the previous constraint forced
the historical-ingestion normalizer to silently drop the second fixture's
stat line for every affected player, corrupting that gameweek's true point
total for future feature engineering.

Widen the constraint to ``(player_id, gameweek_id, fixture_id)`` so both
fixtures of a double gameweek get their own row; a genuine duplicate row
(same player/gameweek/fixture appearing twice in the source data) is still
caught and deduplicated at the application layer before insert (see
``app/ingestion/historical_normalization.py``).

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-12

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("uq_player_gameweek_stats_player_gameweek", "player_gameweek_stats", type_="unique")
    op.create_unique_constraint(
        "uq_player_gameweek_stats_player_gameweek_fixture",
        "player_gameweek_stats",
        ["player_id", "gameweek_id", "fixture_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_player_gameweek_stats_player_gameweek_fixture", "player_gameweek_stats", type_="unique"
    )
    op.create_unique_constraint(
        "uq_player_gameweek_stats_player_gameweek", "player_gameweek_stats", ["player_id", "gameweek_id"]
    )
