"""The ``chip_schedule_optimization_runs`` table: one row per CHIP_SCHEDULE_OPTIMIZATION job run.

Phase 1 of the joint chip scheduler (see ``app.jobs.handlers.chip_schedule_optimization``'s own
module docstring for the full incident/design rationale). Real gap this closes: ``CHIP_SIMULATION``
evaluates each of the 4 chips completely independently, so it can (and did, live) recommend
"use now" for 3 different chips in the SAME gameweek -- a real FPL manager can only play one chip
per gameweek. This job consumes an existing, already-SUCCEEDED ``chip_optimization_runs`` row's
own ``chip_window_candidates`` (fixed, already-computed gain estimates -- no re-evaluation here,
that is Phase 2's job) and picks the best non-colliding gameweek-to-chip assignment across all 4.

``source_chip_optimization_run_id`` was real, required provenance in Phase 1 (a caller reviewing an
assignment needed a direct path back to exactly which ``CHIP_SIMULATION`` run its candidate gains
came from). **Revised for Phase 2** (migration ``0048``): Phase 2 does its own fresh, full-season
evaluation directly from a ``manager_squad_snapshot_id`` -- mirroring ``CHIP_SIMULATION`` itself --
rather than reading an existing run's persisted candidates, so there is no longer always a real
source run to point back to. Nullable and unpopulated for every Phase-2-shaped row going forward;
kept (not dropped) for any historical Phase-1-shaped row and for a caller that happens to have a
relevant ``CHIP_SIMULATION`` run to cross-reference.

Structurally the same run-header shape as ``ChipOptimizationRun`` (own top-level fields are
mostly bookkeeping; the real result lives in the child ``chip_schedule_assignments`` rows) --
``solver_status``/``total_expected_gain`` are this row's own scalars because, unlike
``ChipOptimizationRun``, this run genuinely produces ONE combinatorial-optimization result (one
assignment MIP solve), not 4 independently-evaluated chip results.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.domain.chips import ChipScheduleRunStatus
from packages.domain.optimization import SolverStatus


class ChipScheduleOptimizationRun(Base):
    __tablename__ = "chip_schedule_optimization_runs"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("jobs.id"))
    source_chip_optimization_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("chip_optimization_runs.id")
    )
    manager_squad_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("manager_squad_snapshots.id"), nullable=False
    )
    season_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("seasons.id"), nullable=False
    )
    run_status: Mapped[ChipScheduleRunStatus] = mapped_column(
        SAEnum(
            ChipScheduleRunStatus,
            name="chip_schedule_optimization_run_status",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
        default=ChipScheduleRunStatus.RUNNING,
    )
    solver_status: Mapped[SolverStatus | None] = mapped_column(
        SAEnum(
            SolverStatus,
            name="chip_schedule_optimization_run_solver_status",
            native_enum=False,
            validate_strings=True,
        )
    )
    total_expected_gain: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
