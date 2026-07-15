"""The ``chip_recommendations`` table: one row per evaluated chip type, per ``chip_optimization_runs`` row.

Insert-only child (``ON DELETE CASCADE``, mirrors ``ChipWindowCandidate``'s own
run-child convention). A SUCCEEDED ``chip_optimization_runs`` row has exactly
one of these per requested ``ChipType`` (all 4 when the job's own
``parameters.chip_types`` is omitted) — see
``app.jobs.handlers.chip_simulation`` for the exact per-chip flow that
produces one of these either directly (a chip whose current scheduling window
is USED/ACTIVE_THIS_GAMEWEEK/EXPIRED_UNUSED, no candidates evaluated at all)
or via ``app.chips.scenario.rank_candidates_for_chip`` (a chip whose current
window is genuinely ``AVAILABLE``, with ``chip_window_candidates`` rows
backing it).

``recommended_candidate_id`` is populated only for ``verdict in (USE_NOW,
HOLD)`` — every other verdict (``ALREADY_USED``/``EXPIRED_UNUSED``/
``UNAVAILABLE``/``NO_VIABLE_WINDOW``) means there is no live candidate
gameweek to point at, so it stays ``NULL``.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Text, UniqueConstraint, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.domain.chips import ChipType, ChipVerdict


class ChipRecommendation(Base):
    __tablename__ = "chip_recommendations"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    chip_optimization_run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("chip_optimization_runs.id", ondelete="CASCADE"), nullable=False
    )
    chip_type: Mapped[ChipType] = mapped_column(
        SAEnum(ChipType, name="chip_recommendation_chip_type", native_enum=False, validate_strings=True),
        nullable=False,
    )
    verdict: Mapped[ChipVerdict] = mapped_column(
        SAEnum(ChipVerdict, name="chip_recommendation_verdict", native_enum=False, validate_strings=True),
        nullable=False,
    )
    recommended_candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("chip_window_candidates.id")
    )
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "chip_optimization_run_id", "chip_type", name="uq_chip_recommendations_run_chip_type"
        ),
        Index("ix_chip_recommendations_run_id", "chip_optimization_run_id"),
    )
