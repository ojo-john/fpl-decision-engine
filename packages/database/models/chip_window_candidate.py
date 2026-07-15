"""The ``chip_window_candidates`` table: one row per evaluated candidate gameweek, per chip, per run.

Insert-only child of one ``chip_optimization_runs`` row (``ON DELETE
CASCADE``, mirrors ``TransferOptimizationRunScenario``/``H2HStrategyCandidate``'s
own run-child convention). A chip whose current scheduling window is
``AVAILABLE`` (see ``app.chips.inventory.UserChipWindowStatus``) gets one row
per candidate gameweek evaluated by ``app.chips.scenario`` — every unfinished
gameweek in the window for Bench Boost/Triple Captain (exhaustive), or the
small, cost-bounded subset for Wildcard/Free Hit (see that module's own
"scenario ranking, not perfect optimization" docstring). A chip whose current
window is USED/ACTIVE_THIS_GAMEWEEK/EXPIRED_UNUSED gets NO rows here at all —
only a ``chip_recommendations`` row with the matching verdict (see
``app.jobs.handlers.chip_simulation``).

``rank_within_chip_type`` (1 = best ``gain_mean``) is computed once, at
persist time — every candidate stays visible here for transparency/comparison
(mirrors ``TransferOptimizationRunScenario`` always keeping all 3 evaluated
scenarios), not just the single recommended one.

``is_blank_gameweek``/``is_double_gameweek`` are league-wide facts about this
one candidate gameweek (at least one team in a Blank/Double Gameweek — see
``app.optimization.fixture_calendar.TeamFixtureCount``), computed the same way
regardless of chip type; nullable only in case fixture data for that gameweek
could not be resolved at all (should not happen for any gameweek Layer 2 could
otherwise evaluate, but never crashes over it).
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean as SABoolean
from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, UniqueConstraint, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.domain.chips import ChipType


class ChipWindowCandidate(Base):
    __tablename__ = "chip_window_candidates"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    chip_optimization_run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("chip_optimization_runs.id", ondelete="CASCADE"), nullable=False
    )
    chip_type: Mapped[ChipType] = mapped_column(
        SAEnum(ChipType, name="chip_window_candidate_chip_type", native_enum=False, validate_strings=True),
        nullable=False,
    )
    window_number: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_fpl_event_id: Mapped[int] = mapped_column(Integer, nullable=False)
    is_blank_gameweek: Mapped[bool | None] = mapped_column(SABoolean)
    is_double_gameweek: Mapped[bool | None] = mapped_column(SABoolean)
    gain_mean: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    gain_floor: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    gain_ceiling: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    sample_count: Mapped[int | None] = mapped_column(Integer)
    rank_within_chip_type: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "chip_optimization_run_id",
            "chip_type",
            "candidate_fpl_event_id",
            name="uq_chip_window_candidates_run_chip_event",
        ),
        Index("ix_chip_window_candidates_run_id", "chip_optimization_run_id"),
    )
