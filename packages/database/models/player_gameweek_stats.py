"""The ``player_gameweek_stats`` table: per-gameweek historical stat lines for a player.

Populated by Milestone 3 Layer 1's historical data ingestion (see
``app/ingestion/historical_normalization.py``), sourced from
`vaastav/Fantasy-Premier-League <https://github.com/vaastav/Fantasy-Premier-League>`_'s
``gws/merged_gw.csv`` per season (credit: @vaastav for the scraper/repackaging,
and fantasy.premierleague.com for the underlying data — see that module's
docstring for the full attribution/licensing note). One row per
``(player_id, gameweek_id, fixture_id)`` — a player who didn't feature in a
gameweek has no row here at all (never a zero-filled row), so absence is
distinguishable from "played and recorded a zero". ``fixture_id`` is part of
the key (not just ``player_id``/``gameweek_id``) so a Double Gameweek — a
player with two fixtures in the same round — gets a row per fixture instead
of one being silently dropped (see migration 0008).

Same quarantine convention as ``players``/``fixtures``: a row that fails a
row-level data quality check is still inserted (``is_quarantined=True``,
``quarantine_reason`` set), never dropped outright.

Columns absent from a given season's source schema (see the historical
ingestion module docstring for the exact schema-era breakdown) are always
``NULL``, never defaulted to ``0`` — a missing feature is genuinely unknown,
not zero. ``extra_stats_json`` is a catch-all for any source column not
promoted to its own typed column here.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean as SABoolean
from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base


class PlayerGameweekStats(Base):
    __tablename__ = "player_gameweek_stats"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    player_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("players.id"), nullable=False
    )
    season_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("seasons.id"), nullable=False
    )
    gameweek_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("gameweeks.id"), nullable=False
    )
    fixture_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("fixtures.id"))
    opponent_team_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("teams.id"))
    was_home: Mapped[bool | None] = mapped_column(SABoolean)
    position_short_name_at_time: Mapped[str | None] = mapped_column(String(10))

    minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    goals_scored: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    assists: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    clean_sheets: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    goals_conceded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    own_goals: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    penalties_saved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    penalties_missed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    yellow_cards: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    red_cards: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    saves: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bonus: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    ict_index: Mapped[Decimal | None] = mapped_column(Numeric(6, 1))
    influence: Mapped[Decimal | None] = mapped_column(Numeric(6, 1))
    creativity: Mapped[Decimal | None] = mapped_column(Numeric(6, 1))
    threat: Mapped[Decimal | None] = mapped_column(Numeric(6, 1))

    expected_goals: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    expected_assists: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    expected_goal_involvements: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    expected_goals_conceded: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    starts: Mapped[bool | None] = mapped_column(SABoolean)

    defensive_contribution: Mapped[int | None] = mapped_column(Integer)
    clearances_blocks_interceptions: Mapped[int | None] = mapped_column(Integer)
    recoveries: Mapped[int | None] = mapped_column(Integer)
    tackles: Mapped[int | None] = mapped_column(Integer)

    xp_estimate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    value_at_time: Mapped[int | None] = mapped_column(Integer)
    # Raw ownership COUNT at the time of this gameweek — distinct from
    # players.selected_by_percent (a percentage). Never conflate the two.
    selected_by_count_at_time: Mapped[int | None] = mapped_column(Integer)
    transfers_in: Mapped[int | None] = mapped_column(Integer)
    transfers_out: Mapped[int | None] = mapped_column(Integer)
    transfers_balance: Mapped[int | None] = mapped_column(Integer)

    data_source: Mapped[str] = mapped_column(String(50), nullable=False)
    extra_stats_json: Mapped[dict | None] = mapped_column(JSONB)

    is_quarantined: Mapped[bool] = mapped_column(SABoolean, nullable=False, default=False)
    quarantine_reason: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        # Includes fixture_id (not just player_id/gameweek_id) so a Double
        # Gameweek — a player with two fixtures in the same gameweek round,
        # a real and recurring FPL mechanic — gets a row per fixture instead
        # of silently dropping the second one. See migration 0008.
        UniqueConstraint(
            "player_id",
            "gameweek_id",
            "fixture_id",
            name="uq_player_gameweek_stats_player_gameweek_fixture",
        ),
        Index("ix_player_gameweek_stats_season_id", "season_id"),
        Index("ix_player_gameweek_stats_gameweek_id", "gameweek_id"),
    )
