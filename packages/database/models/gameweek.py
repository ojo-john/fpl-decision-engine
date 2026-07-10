"""The ``gameweeks`` table: normalized FPL events for a season.

No ``is_quarantined`` here (unlike players/fixtures) — a bad gameweek row
(e.g. two rows both claiming ``is_current``) is a systemic/critical failure
that should block the whole run via the data quality gate, not a per-row
concern to silently route around.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean as SABoolean
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base


class Gameweek(Base):
    __tablename__ = "gameweeks"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    fpl_event_id: Mapped[int] = mapped_column(Integer, nullable=False)
    season_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("seasons.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    deadline_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished: Mapped[bool] = mapped_column(SABoolean, nullable=False, default=False)
    data_checked: Mapped[bool] = mapped_column(SABoolean, nullable=False, default=False)
    is_previous: Mapped[bool] = mapped_column(SABoolean, nullable=False, default=False)
    is_current: Mapped[bool] = mapped_column(SABoolean, nullable=False, default=False)
    is_next: Mapped[bool] = mapped_column(SABoolean, nullable=False, default=False)
    average_entry_score: Mapped[int | None] = mapped_column(Integer)
    highest_score: Mapped[int | None] = mapped_column(Integer)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("season_id", "fpl_event_id", name="uq_gameweeks_season_fpl_event_id"),
        Index("ix_gameweeks_deadline_time", "deadline_time"),
        Index(
            "ix_gameweeks_season_is_current",
            "season_id",
            unique=True,
            postgresql_where=text("is_current"),
        ),
    )
