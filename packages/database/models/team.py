"""The ``teams`` table: normalized Premier League club data for a season."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean as SABoolean
from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    fpl_team_id: Mapped[int] = mapped_column(Integer, nullable=False)
    season_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("seasons.id"), nullable=False
    )
    code: Mapped[int | None] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    short_name: Mapped[str] = mapped_column(String(10), nullable=False)
    strength: Mapped[int | None] = mapped_column(Integer)
    strength_overall_home: Mapped[int | None] = mapped_column(Integer)
    strength_overall_away: Mapped[int | None] = mapped_column(Integer)
    strength_attack_home: Mapped[int | None] = mapped_column(Integer)
    strength_attack_away: Mapped[int | None] = mapped_column(Integer)
    strength_defence_home: Mapped[int | None] = mapped_column(Integer)
    strength_defence_away: Mapped[int | None] = mapped_column(Integer)
    played: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    win: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    draw: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    loss: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    position: Mapped[int | None] = mapped_column(Integer)
    unavailable: Mapped[bool] = mapped_column(SABoolean, nullable=False, default=False)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("season_id", "fpl_team_id", name="uq_teams_season_fpl_team_id"),)
