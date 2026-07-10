"""The ``fixtures`` table: normalized FPL fixtures for a season.

``gameweek_id`` is nullable — FPL fixtures can have ``event=null`` for
blank/TBD gameweeks (rescheduled fixtures not yet assigned to a gameweek).
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean as SABoolean
from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base


class Fixture(Base):
    __tablename__ = "fixtures"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    fpl_fixture_id: Mapped[int] = mapped_column(Integer, nullable=False)
    season_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("seasons.id"), nullable=False
    )
    gameweek_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("gameweeks.id"))
    home_team_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("teams.id"))
    away_team_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("teams.id"))
    kickoff_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished: Mapped[bool] = mapped_column(SABoolean, nullable=False, default=False)
    started: Mapped[bool] = mapped_column(SABoolean, nullable=False, default=False)
    home_score: Mapped[int | None] = mapped_column(Integer)
    away_score: Mapped[int | None] = mapped_column(Integer)
    home_difficulty: Mapped[int | None] = mapped_column(Integer)
    away_difficulty: Mapped[int | None] = mapped_column(Integer)
    is_quarantined: Mapped[bool] = mapped_column(SABoolean, nullable=False, default=False)
    quarantine_reason: Mapped[str | None] = mapped_column(Text)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("uq_fixtures_season_fpl_fixture_id", "season_id", "fpl_fixture_id", unique=True),
        Index("ix_fixtures_gameweek_id", "gameweek_id"),
        Index("ix_fixtures_home_team_id", "home_team_id"),
        Index("ix_fixtures_away_team_id", "away_team_id"),
        Index("ix_fixtures_kickoff_time", "kickoff_time"),
    )
