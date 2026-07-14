"""The ``h2h_simulation_runs`` table: one row per H2H_SIMULATION job run.

Milestone 5 Layer 3 (H2H Strategy: matchup simulation). Structurally the same
run-header shape as ``TransferOptimizationRun`` (see that model's own
docstring) — snapshotted rules/data-quality/gate context, nullable-until-
success result fields, and a ``parameters_json`` audit trail — but insert-only
and one level deep: a single row IS the whole result (summary statistics
only), there is no per-scenario/per-move child table here, because
``app.h2h.simulate.simulate_matchup`` returns one paired-draw distribution
for one (user squad, rival squad) matchup, not several competing scenarios.

``manager_squad_snapshot_id``/``rival_profile_id`` are the two required
inputs (the user's own squad state and the rival's derived/predicted state,
Milestone 5 Layers 1-2's outputs respectively). ``h2h_match_fpl_id`` is
optional, plain-Integer provenance (which real ``h2h_matches.fpl_match_id``
this run was checking, if any) — deliberately NOT a foreign key: a
simulation is meaningfully runnable for a hypothetical/future matchup that
has no corresponding synced ``h2h_matches`` row yet, or against a manager
pairing outside any synced league at all.

``target_fpl_event_id`` is always the raw FPL event number actually
simulated (never the internal gameweek UUID) — mirrors
``resolve_most_recent_finished_gameweek_event``'s own raw-event-number
convention, since this is also what a caller supplies back as
``parameters.target_fpl_event_id`` for a past-completed-gameweek
plausibility check.

``confidence`` reuses Layer 2's ``RivalStateConfidence`` (not a new,
simulation-specific enum) — see ``app.h2h.simulate``'s own docstring for why
this is always hard-floored at the input ``RivalProfile.overall_confidence``
rather than computed independently.
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
from packages.domain.h2h import H2HSimulationRunStatus, RivalStateConfidence
from packages.domain.rules import RulesStatus


class H2HSimulationRun(Base):
    __tablename__ = "h2h_simulation_runs"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("jobs.id"))
    manager_squad_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("manager_squad_snapshots.id"), nullable=False
    )
    rival_profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("rival_profiles.id"), nullable=False
    )
    # Provenance only, deliberately not a FK -- see module docstring.
    h2h_match_fpl_id: Mapped[int | None] = mapped_column(Integer)
    run_status: Mapped[H2HSimulationRunStatus] = mapped_column(
        SAEnum(
            H2HSimulationRunStatus,
            name="h2h_simulation_run_status",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
        default=H2HSimulationRunStatus.RUNNING,
    )
    target_fpl_event_id: Mapped[int] = mapped_column(Integer, nullable=False)
    simulation_method_version: Mapped[int] = mapped_column(Integer, nullable=False)
    sample_count: Mapped[int | None] = mapped_column(Integer)
    user_score_mean: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    user_score_floor: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    user_score_ceiling: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    rival_score_mean: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    rival_score_floor: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    rival_score_ceiling: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    win_probability: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    draw_probability: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    loss_probability: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    expected_margin: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    margin_std_dev: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    confidence: Mapped[RivalStateConfidence | None] = mapped_column(
        SAEnum(
            RivalStateConfidence,
            name="h2h_simulation_run_confidence",
            native_enum=False,
            validate_strings=True,
        )
    )
    rules_status_snapshot: Mapped[RulesStatus] = mapped_column(
        SAEnum(
            RulesStatus,
            name="h2h_simulation_run_rules_status_snapshot",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
    )
    optimization_permitted_snapshot: Mapped[bool] = mapped_column(SABoolean, nullable=False)
    data_quality_score_snapshot: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    parameters_json: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
