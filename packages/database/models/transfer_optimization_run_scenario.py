"""The ``transfer_optimization_run_scenarios`` table: one row per evaluated 0/1/2-transfer scenario.

A SUCCEEDED ``transfer_optimization_runs`` row always has exactly 3 of these
(``transfers_count`` 0, 1, 2 — see ``app.transfers.scenario_search.evaluate_transfer_scenarios``),
so every candidate scenario stays visible for transparency/comparison, not
just the single recommended one. Individual player moves within a scenario
live in ``transfer_optimization_run_moves``.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean as SABoolean
from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base


class TransferOptimizationRunScenario(Base):
    __tablename__ = "transfer_optimization_run_scenarios"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    transfer_optimization_run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("transfer_optimization_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    transfers_count: Mapped[int] = mapped_column(Integer, nullable=False)  # 0, 1, or 2
    raw_horizon_points_gain: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    hit_cost: Mapped[int] = mapped_column(Integer, nullable=False)
    net_horizon_points_gain: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    is_recommended: Mapped[bool] = mapped_column(SABoolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "transfer_optimization_run_id", "transfers_count", name="uq_transfer_opt_run_scenarios_run_count"
        ),
        Index("ix_transfer_opt_run_scenarios_run_id", "transfer_optimization_run_id"),
    )
