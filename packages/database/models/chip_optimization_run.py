"""The ``chip_optimization_runs`` table: one row per CHIP_SIMULATION job run.

Milestone 6 Layer 3 — wires Layer 1 (``app.chips.windows``/``app.chips.inventory``:
chip scheduling windows and the user's own play-history classification) and Layer 2
(``app.chips.scenario``: per-chip gain evaluation and USE_NOW/HOLD/NO_VIABLE_WINDOW
ranking) into a persisted job handler. Structurally the same run-header shape as
``TransferOptimizationRun``/``H2HSimulationRun`` (see those models' own docstrings)
— snapshotted rules/data-quality/gate context, and a ``parameters_json`` audit
trail — but one level deep from the run's OWN top-level fields: a run's result is
entirely captured by its child ``chip_recommendations``/``chip_window_candidates``
rows (one recommendation per evaluated chip type, each with its own evaluated
candidate gameweeks), not by scalar columns on this row itself, because a single
run evaluates up to 4 independent chips at once (unlike ``H2HSimulationRun``,
which is one matchup's own single result).

``rules_config_id`` pins the EXACT chip-window scheduling data
(``RulesConfig.raw_chips_json``) this run resolved its windows from — a
``rules_config`` version bump (see ``app.rules.sync``) between two runs could
plausibly shift chip window boundaries, so this is real provenance, not
incidental audit trail.

``target_fpl_event_id`` is the next unfinished gameweek this run was computed
as-of (raw FPL event number, mirrors ``H2HSimulationRun.target_fpl_event_id``'s
own raw-event-number convention) — always resolved fresh per run, never a
caller-supplied override (unlike ``H2HSimulationRun``, chip timing has no
meaningful "check a past gameweek" use case, so there is no override
parameter to snapshot here). Off-season (no unfinished gameweek at all) is a
hard refusal for this job (see ``app.jobs.handlers.chip_simulation``'s own
docstring for why this mirrors ``TRANSFER_OPTIMIZATION``'s off-season
handling, not ``H2H_SIMULATION``'s softer fallback) — so this column is
always populated for a row that exists at all.
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
from packages.domain.chips import ChipOptimizationRunStatus
from packages.domain.rules import RulesStatus


class ChipOptimizationRun(Base):
    __tablename__ = "chip_optimization_runs"

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
    run_status: Mapped[ChipOptimizationRunStatus] = mapped_column(
        SAEnum(
            ChipOptimizationRunStatus,
            name="chip_optimization_run_status",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
        default=ChipOptimizationRunStatus.RUNNING,
    )
    target_fpl_event_id: Mapped[int] = mapped_column(Integer, nullable=False)
    projection_formula_version: Mapped[int] = mapped_column(Integer, nullable=False)
    rules_status_snapshot: Mapped[RulesStatus] = mapped_column(
        SAEnum(
            RulesStatus,
            name="chip_optimization_run_rules_status_snapshot",
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
