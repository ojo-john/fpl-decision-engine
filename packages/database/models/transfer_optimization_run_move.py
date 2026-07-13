"""The ``transfer_optimization_run_moves`` table: one row per (player_out, player_in) swap
within a ``transfer_optimization_run_scenarios`` row.

A 0-transfer (ROLL) scenario has zero moves; a 1-transfer scenario has one
(``move_index=0``); a 2-transfer scenario has two (``move_index`` 0 and 1).
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base


class TransferOptimizationRunMove(Base):
    __tablename__ = "transfer_optimization_run_moves"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    transfer_optimization_run_scenario_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("transfer_optimization_run_scenarios.id", ondelete="CASCADE"),
        nullable=False,
    )
    move_index: Mapped[int] = mapped_column(Integer, nullable=False)  # 0 or 1 within a scenario
    player_out_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("players.id"), nullable=False
    )
    player_out_selling_price_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    player_in_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("players.id"), nullable=False
    )
    player_in_cost_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    projected_points_gain_over_horizon: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_transfer_opt_run_moves_scenario_id", "transfer_optimization_run_scenario_id"),
    )
