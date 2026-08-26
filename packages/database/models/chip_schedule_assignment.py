"""The ``chip_schedule_assignments`` table: one row per chip type, per CHIP_SCHEDULE_OPTIMIZATION run.

Insert-only child of one ``chip_schedule_optimization_runs`` row (``ON DELETE CASCADE``, mirrors
``ChipWindowCandidate``'s own run-child convention). Exactly one row is written per requested chip
type per run (unlike ``ChipWindowCandidate``, which has one row per evaluated CANDIDATE gameweek) --
this table's own job is to record the SOLVER'S final decision for that chip, not every candidate it
considered (those already exist, unchanged, on the source run's own ``chip_window_candidates``
rows, reachable via ``source_chip_window_candidate_id`` below).

``assigned_fpl_event_id``/``gain_mean``/``source_chip_window_candidate_id`` are all NULL together
when the solver determined this chip is not worth playing within the evaluated candidates (e.g.
every candidate's own ``gain_mean`` was <= 0, or the assignment MIP found a higher-total-value
combination that leaves this chip unused this round) -- a real, valid outcome, not a partial/error
state.

``window_number`` is included from Phase 1 onward even though Phase 1 only ever evaluates a chip's
CURRENTLY open window (window 1 or 2, whichever ``CHIP_SIMULATION``'s own window resolution
selected) -- Phase 2 (full-season, both-windows joint solve) reads/writes both without a schema
migration.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, UniqueConstraint, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.domain.chips import ChipType


class ChipScheduleAssignment(Base):
    __tablename__ = "chip_schedule_assignments"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    chip_schedule_optimization_run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("chip_schedule_optimization_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    chip_type: Mapped[ChipType] = mapped_column(
        SAEnum(ChipType, name="chip_schedule_assignment_chip_type", native_enum=False, validate_strings=True),
        nullable=False,
    )
    window_number: Mapped[int] = mapped_column(Integer, nullable=False)
    assigned_fpl_event_id: Mapped[int | None] = mapped_column(Integer)
    gain_mean: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    source_chip_window_candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("chip_window_candidates.id")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "chip_schedule_optimization_run_id",
            "chip_type",
            "window_number",
            name="uq_chip_schedule_assignments_run_chip_window",
        ),
        Index("ix_chip_schedule_assignments_run_id", "chip_schedule_optimization_run_id"),
    )
