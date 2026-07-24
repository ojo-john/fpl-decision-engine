"""The ``players`` table: normalized FPL elements for a season.

Row-level bad data here (a plausible, likely occurrence — a single player
row can be wrong without indicating a systemic ingestion failure) is
handled via quarantine (``is_quarantined``/``quarantine_reason``) rather
than blocking the whole run. Always query ``WHERE NOT is_quarantined`` for
"active" player data — see ``app/ingestion/normalization.py`` module
docstring for the full quarantine convention.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean as SABoolean
from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base


class Player(Base):
    __tablename__ = "players"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    fpl_element_id: Mapped[int] = mapped_column(Integer, nullable=False)
    fpl_code: Mapped[int | None] = mapped_column(Integer)
    season_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("seasons.id"), nullable=False
    )
    team_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("teams.id"))
    position_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("positions.id"))
    first_name: Mapped[str | None] = mapped_column(String(100))
    second_name: Mapped[str | None] = mapped_column(String(100))
    web_name: Mapped[str | None] = mapped_column(String(100))
    now_cost: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str | None] = mapped_column(String(5))
    total_points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
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
    selected_by_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 1))
    # This gameweek's live transfer momentum (FPL resets both to 0 at the start of each
    # gameweek) -- distinct from player_gameweek_stats.transfers_in/transfers_out, which are
    # season-cumulative totals sourced from the historical/element-summary pipeline, not a
    # live in-gameweek signal. Used by app.players.price_signal for price-change heuristics.
    transfers_in_event: Mapped[int | None] = mapped_column(Integer)
    transfers_out_event: Mapped[int | None] = mapped_column(Integer)
    cost_change_event: Mapped[int | None] = mapped_column(Integer)
    chance_of_playing_next_round: Mapped[int | None] = mapped_column(Integer)
    chance_of_playing_this_round: Mapped[int | None] = mapped_column(Integer)
    points_per_game: Mapped[Decimal | None] = mapped_column(Numeric(5, 1))
    form: Mapped[Decimal | None] = mapped_column(Numeric(5, 1))
    ep_this: Mapped[Decimal | None] = mapped_column(Numeric(5, 1))
    ep_next: Mapped[Decimal | None] = mapped_column(Numeric(5, 1))
    starts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    news: Mapped[str | None] = mapped_column(Text)
    is_quarantined: Mapped[bool] = mapped_column(SABoolean, nullable=False, default=False)
    quarantine_reason: Mapped[str | None] = mapped_column(Text)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("season_id", "fpl_element_id", name="uq_players_season_fpl_element_id"),
        Index("ix_players_fpl_code", "fpl_code"),
        Index("ix_players_team_id", "team_id"),
        Index("ix_players_position_id", "position_id"),
    )
