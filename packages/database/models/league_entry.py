"""The ``league_entries`` table: one row per (league, manager) standings entry, upserted in place.

Upsert-in-place/current-state semantics, same rationale as ``leagues`` — a
manager's rank/points-for in a league is real external state with exactly
one current true value; historical trend across gameweeks is not tracked
here (that would need its own snapshot table if a later layer wants it).
Keyed on ``(league_id, fpl_manager_id)``, not a single-column unique key,
because the same manager can appear in several leagues.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base


class LeagueEntry(Base):
    __tablename__ = "league_entries"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    league_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("leagues.id"), nullable=False
    )
    fpl_manager_id: Mapped[int] = mapped_column(Integer, nullable=False)
    player_name: Mapped[str | None] = mapped_column(String)
    entry_name: Mapped[str | None] = mapped_column(String)
    rank: Mapped[int | None] = mapped_column(Integer)
    last_rank: Mapped[int | None] = mapped_column(Integer)
    matches_played: Mapped[int | None] = mapped_column(Integer)
    matches_won: Mapped[int | None] = mapped_column(Integer)
    matches_drawn: Mapped[int | None] = mapped_column(Integer)
    matches_lost: Mapped[int | None] = mapped_column(Integer)
    points_for: Mapped[int | None] = mapped_column(Integer)
    total: Mapped[int | None] = mapped_column(Integer)
    division: Mapped[int | None] = mapped_column(Integer)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("league_id", "fpl_manager_id", name="uq_league_entries_league_manager"),
        Index("ix_league_entries_league_id", "league_id"),
    )
