"""The ``league_sync_runs`` table: one row per LEAGUE_SYNC job run, insert-only run-header.

Mirrors ``ingestion_runs``'s shape/role (see that model's docstring) — a
run-header row created early (before per-rival fetching begins, matching
``SquadOptimizationJobRunner``'s discipline of creating its own run row up
front) and finalized to a terminal ``LeagueSyncRunStatus`` once the job
completes or fails.

``fpl_api_health_snapshot`` reuses ``packages.domain.ingestion.EndpointHealth``
as an enum column (native_enum=False, validate_strings=True) — the same type
``ingestion_runs.overall_health`` already uses for this exact concept
(worst/overall health observed across a run's fetches), not a plain
``String`` column. This is a deliberate deviation from a plain string type:
"reuse whatever type ingestion_run.py already uses for this concept" was the
instruction, and what it actually uses is this enum, not a string.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.domain.h2h import LeagueSyncRunStatus
from packages.domain.ingestion import EndpointHealth


class LeagueSyncRun(Base):
    __tablename__ = "league_sync_runs"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("jobs.id"))
    # Nullable in case league resolution itself fails before a League row is upserted.
    league_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("leagues.id"))
    season_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("seasons.id"), nullable=False
    )
    run_status: Mapped[LeagueSyncRunStatus] = mapped_column(
        SAEnum(LeagueSyncRunStatus, name="league_sync_run_status", native_enum=False, validate_strings=True),
        nullable=False,
        default=LeagueSyncRunStatus.RUNNING,
    )
    rivals_synced_count: Mapped[int | None] = mapped_column(Integer)
    rivals_unavailable_count: Mapped[int | None] = mapped_column(Integer)
    fpl_api_health_snapshot: Mapped[EndpointHealth | None] = mapped_column(
        SAEnum(
            EndpointHealth,
            name="league_sync_run_fpl_api_health_snapshot",
            native_enum=False,
            validate_strings=True,
        )
    )
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
