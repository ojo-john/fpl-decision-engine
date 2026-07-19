"""The ``learning_reports`` table: one row per GAMEWEEK_REVIEW job run, insert-only.

Milestone 7 Layer 4 (Recursive Learning Loop: GAMEWEEK_REVIEW orchestration).
This is the layer that wires Layers 1-3 together end to end: given an
``is_authoritative`` ``recommendation_audits`` row whose target gameweek has
finished, ``app.jobs.handlers.gameweek_review`` calls (in order)
``app.learning.outcome_ingestion.ingest_outcome_for_audit`` (Layer 2),
``app.learning.attribution.attribute_failures_for_outcome`` (Layer 3), and
all 4 of ``app.learning.signals``' own ``compute_*``/``persist_learning_signal``
functions (Layer 3), then persists ONE ``learning_reports`` row summarizing
what happened. See that job handler's own module docstring for the full
orchestration order and error handling.

Insert-only, ONE row per RUN -- never upserted, mirroring ``learning_signals``'/
``ingestion_runs``'s own "each computation is its own recordable event"
convention (NOT ``recommendation_outcomes``'s "converges to one true value"
upsert-in-place convention): a rerun of GAMEWEEK_REVIEW for the same gameweek
is itself worth keeping as its own distinct event (e.g. a rerun after a late
bonus-points correction produced a genuinely different report), even though
the ``recommendation_outcome_id`` it references may point at the SAME
(upserted-in-place) ``recommendation_outcomes`` row across multiple such runs.

Mirrors ``ChipOptimizationRun``/``TransferOptimizationRun``'s own
create-early-RUNNING-row discipline -- a row is created immediately once the
gameweek/audit inputs are confirmed valid, BEFORE the actual Layer 2/3 work
runs, so a crash mid-review still leaves a terminal (FAILED) row behind
rather than none at all.

``recommendation_outcome_id`` is nullable because it is only ever populated
AFTER ``ingest_outcome_for_audit`` succeeds -- a row that fails before that
step completes (e.g. because the audit itself somehow disappeared between
resolution and use) stays NULL there, same convention as
``failure_attribution_count``/``learning_signals_computed_json``/
``report_markdown``/``summary_json``/``notable_findings_json`` all staying
NULL on a FAILED row.

``learning_signals_computed_json`` is a COMPACT summary (which
``LearningSignalType``s were computed this run, plus their key
``value``/``threshold_breached`` fields) -- NOT a full re-embedding of the
real rows, which live in ``learning_signals`` itself (this run's own rows are
always findable there via ``season_id``/``as_of_gameweek_id`` -- the reviewed
gameweek -- and ``created_at`` proximity to this row's own ``completed_at``).

``notable_findings_json`` is a structured, BOUNDED list built MECHANICALLY
from already-computed structured data (never free-text-generated) -- one
entry per non-``RECOMMENDATION_WAS_SOUND`` ``FailureAttribution`` this run
produced, plus one entry per ``threshold_breached=True`` learning signal this
run computed. See ``app.learning.report.compose_notable_findings`` for the
exact, pure composition logic (Section 31's "notify only on material
findings" principle -- an all-sound, no-breach review yields an EMPTY list
here, not a manufactured "nothing to report" entry).

Milestone 7 Layer 6 (Recursive Learning Loop: BACKTEST job + the
GAMEWEEK_REVIEW -> MODEL_TRAINING retraining-trigger wiring) adds
``retraining_triggered``/``retraining_job_id`` below. Once this run's own
``ROLLING_PROJECTION_MAE`` learning signal comes back with
``threshold_breached=True``, ``app.jobs.handlers.gameweek_review`` triggers a
``MODEL_TRAINING`` job and records the outcome here --
``retraining_triggered=True`` and ``retraining_job_id`` pointing at that
``jobs`` row. Both stay ``NULL`` for the overwhelmingly common case (no
breach this run) and also for any row that never reached this final step
(e.g. a ``FAILED`` row from an earlier failure) -- same "``NULL`` until this
step actually runs" convention already used by
``recommendation_outcome_id``/``failure_attribution_count`` above.
``retraining_triggered=True`` does NOT imply the triggered training (or any
downstream promotion-gate check) succeeded -- see
``summary_json``/``notable_findings_json``/``report_markdown`` for the full
outcome (this job's own retraining-trigger step is explicitly failure-
isolated from GAMEWEEK_REVIEW's own primary success; a training failure is
recorded, never propagated as this row's own ``FAILED`` status).
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean as SABoolean
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.domain.learning import GameweekReviewRunStatus


class LearningReport(Base):
    __tablename__ = "learning_reports"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("jobs.id"))
    season_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("seasons.id"), nullable=False
    )
    gameweek_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("gameweeks.id"), nullable=False
    )
    run_status: Mapped[GameweekReviewRunStatus] = mapped_column(
        SAEnum(
            GameweekReviewRunStatus,
            name="learning_report_run_status",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
        default=GameweekReviewRunStatus.RUNNING,
    )
    recommendation_outcome_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("recommendation_outcomes.id")
    )
    failure_attribution_count: Mapped[int | None] = mapped_column(Integer)
    learning_signals_computed_json: Mapped[dict | None] = mapped_column(JSONB)
    report_markdown: Mapped[str | None] = mapped_column(Text)
    summary_json: Mapped[dict | None] = mapped_column(JSONB)
    notable_findings_json: Mapped[list | None] = mapped_column(JSONB)
    retraining_triggered: Mapped[bool | None] = mapped_column(SABoolean)
    retraining_job_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("jobs.id"))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_learning_reports_season_gameweek", "season_id", "gameweek_id"),
        Index("ix_learning_reports_job_id", "job_id"),
    )
