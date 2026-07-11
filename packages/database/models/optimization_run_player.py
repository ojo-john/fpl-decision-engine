"""The ``optimization_run_players`` table: one row per player within an optimization run's recommendation."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean as SABoolean
from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base


class OptimizationRunPlayer(Base):
    __tablename__ = "optimization_run_players"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    optimization_run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("optimization_runs.id", ondelete="CASCADE"), nullable=False
    )
    player_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("players.id"), nullable=False
    )
    is_starting: Mapped[bool] = mapped_column(SABoolean, nullable=False, default=False)
    bench_order: Mapped[int | None] = mapped_column(Integer)
    is_captain: Mapped[bool] = mapped_column(SABoolean, nullable=False, default=False)
    is_vice_captain: Mapped[bool] = mapped_column(SABoolean, nullable=False, default=False)
    projected_points: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    projection_source: Mapped[str] = mapped_column(String(50), nullable=False)
    now_cost_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("optimization_run_id", "player_id", name="uq_optimization_run_players_run_player"),
        Index("ix_optimization_run_players_run_id", "optimization_run_id"),
    )
