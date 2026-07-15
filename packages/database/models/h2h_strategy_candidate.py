"""The ``h2h_strategy_candidates`` table: one row per evaluated named strategy, per H2H_SIMULATION run.

Milestone 5 Layer 4 (H2H Strategy: risk-adjusted strategy comparison — the
final piece of the master spec's section 20). A SUCCEEDED ``h2h_simulation_runs``
row has between 5 and 7 of these children: always ``EP_OPTIMAL``,
``LOW_VARIANCE``, ``HIGH_UPSIDE``, ``CAPTAIN_COVER``, ``DIFFERENTIAL_CAPTAIN``;
plus ``NO_HIT`` when a prior SUCCEEDED ``TransferOptimizationRun`` exists for
the same ``manager_squad_snapshot_id`` (never fabricated without a real
Milestone 4 scenario row to cite as provenance — no run means 5, not 6);
plus ``CALCULATED_HIT`` additionally when that same run's own recommended
scenario actually clears a hit cost (``transfers_count`` 1 or 2, not a roll).
See ``app.jobs.handlers.h2h_simulation``'s own docstring for the exact
availability/skip rules per candidate.

Insert-only, a pure child of one ``h2h_simulation_runs`` row (``ON DELETE
CASCADE``, mirrors ``TransferOptimizationRunScenario``'s own run-child
convention) — never updated in place once persisted.

``transfer_optimization_run_scenario_id`` is populated only for ``NO_HIT``/
``CALCULATED_HIT`` candidates that reused an actual Milestone 4 scenario row
(never fabricated here — Milestone 4's own scenario search already produced
it). ``captain_player_id`` is populated only when a candidate's actual final
captain (after autosub) differs from ``EP_OPTIMAL``'s own final captain —
``NULL`` means "same captain as the baseline", not "no captain at all".

``is_recommended``/``rationale`` are computed by
``app.h2h.strategy.select_recommended_candidate`` — exactly one row per run
has ``is_recommended=True``.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean as SABoolean
from sqlalchemy import DateTime, ForeignKey, Index, Numeric, Text, UniqueConstraint, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.domain.h2h import H2HStrategyLabel


class H2HStrategyCandidate(Base):
    __tablename__ = "h2h_strategy_candidates"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    h2h_simulation_run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("h2h_simulation_runs.id", ondelete="CASCADE"), nullable=False
    )
    strategy_label: Mapped[H2HStrategyLabel] = mapped_column(
        SAEnum(
            H2HStrategyLabel, name="h2h_strategy_candidate_label", native_enum=False, validate_strings=True
        ),
        nullable=False,
    )
    # Reused verbatim from Milestone 4's own scenario search -- never a FK to
    # a scenario this layer fabricated itself. See module docstring.
    transfer_optimization_run_scenario_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("transfer_optimization_run_scenarios.id")
    )
    # This candidate's FINAL captain (after autosub), only when it differs
    # from EP_OPTIMAL's own final captain -- see module docstring.
    captain_player_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("players.id")
    )
    win_probability: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    draw_probability: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    loss_probability: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    expected_margin: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    is_recommended: Mapped[bool] = mapped_column(SABoolean, nullable=False, default=False)
    rationale: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "h2h_simulation_run_id", "strategy_label", name="uq_h2h_strategy_candidates_run_label"
        ),
        Index("ix_h2h_strategy_candidates_run_id", "h2h_simulation_run_id"),
    )
