"""The ``transfer_optimization_runs`` table: one row per TRANSFER_OPTIMIZATION job run.

Milestone 4 Layer 3's run-header row, structurally the same shape as
``OptimizationRun`` (see that model's docstring) — snapshotted
rules/data-quality/gate context, nullable-until-success result fields, and a
``parameters_json`` audit trail. The concrete 0/1/2-transfer scenarios live in
``transfer_optimization_run_scenarios`` (one row per scenario, always exactly
3 for a SUCCEEDED run), whose individual player moves live in
``transfer_optimization_run_moves`` — a three-level run/scenario/move
hierarchy, one level deeper than ``OptimizationRun``'s run/player shape,
because a single run here evaluates three competing scenarios at once rather
than producing one recommendation outright.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean as SABoolean
from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.domain.rules import RulesStatus
from packages.domain.transfers import TransferOptimizationRunStatus


class TransferOptimizationRun(Base):
    __tablename__ = "transfer_optimization_runs"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("jobs.id"))
    manager_squad_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("manager_squad_snapshots.id"), nullable=False
    )
    season_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("seasons.id"), nullable=False
    )
    rules_config_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("rules_config.id"), nullable=False
    )
    run_status: Mapped[TransferOptimizationRunStatus] = mapped_column(
        SAEnum(
            TransferOptimizationRunStatus,
            name="transfer_optimization_run_status",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
        default=TransferOptimizationRunStatus.RUNNING,
    )
    horizon_start_gameweek_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("gameweeks.id")
    )
    horizon_length: Mapped[int] = mapped_column(Integer, nullable=False)
    # List of gameweek id strings actually resolved by resolve_horizon_gameweeks
    # -- may be shorter than horizon_length near season end.
    horizon_gameweek_ids_json: Mapped[list | None] = mapped_column(JSONB)
    free_transfers_available_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    bank_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    transfer_hit_cost_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)  # positive magnitude
    recommended_transfers_count: Mapped[int | None] = mapped_column(Integer)
    recommended_hit_cost: Mapped[int | None] = mapped_column(Integer)
    recommended_net_horizon_points_gain: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    optimization_permitted_snapshot: Mapped[bool] = mapped_column(SABoolean, nullable=False)
    data_quality_score_snapshot: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    rules_status_snapshot: Mapped[RulesStatus] = mapped_column(
        SAEnum(
            RulesStatus,
            name="transfer_optimization_run_rules_status_snapshot",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
    )
    projection_formula_version: Mapped[int] = mapped_column(Integer, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    parameters_json: Mapped[dict | None] = mapped_column(JSONB)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
