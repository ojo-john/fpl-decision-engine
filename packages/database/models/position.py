"""The ``positions`` table: normalized FPL element_types (GKP/DEF/MID/FWD) for a season."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    fpl_element_type_id: Mapped[int] = mapped_column(Integer, nullable=False)
    season_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("seasons.id"), nullable=False
    )
    singular_name: Mapped[str] = mapped_column(String(50), nullable=False)
    short_name: Mapped[str] = mapped_column(String(10), nullable=False)
    squad_select: Mapped[int] = mapped_column(Integer, nullable=False)
    squad_min_play: Mapped[int] = mapped_column(Integer, nullable=False)
    squad_max_play: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("season_id", "fpl_element_type_id", name="uq_positions_season_fpl_element_type_id"),
    )
