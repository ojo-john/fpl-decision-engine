"""The ``recommendation_audits`` table: one row per RECOMMENDATION_GENERATION job run.

Milestone 7 Layer 1 (Recursive Learning Loop: Recommendation Correlation/
Audit) -- the first piece of Milestone 7. A row does NOT duplicate any of the
5 existing recommendation-producing job handlers' own persisted data
(``optimization_runs``/``transfer_optimization_runs``/``h2h_simulation_runs``/
``chip_optimization_runs``, see each of those models' own docstrings) -- it
only CORRELATES the most recent SUCCEEDED run of each type for one target
gameweek via a nullable FK, plus records generation-time context
(``fpl_api_health_at_generation``, ``champion_model_version_id``,
``application_version``) and a mechanically-derived confidence/assumptions/
known-risks summary. See ``app.jobs.handlers.recommendation_generation`` for
the full resolution logic.

Insert-only/audit-snapshot semantics for every column EXCEPT
``user_actual_decision_json`` -- mirrors ``manager_squad_snapshots``'s own
"each generation is its own recordable event, never upserted in place"
convention (see that model's docstring), with the SAME ``is_authoritative``
supersession technique: a fresh RECOMMENDATION_GENERATION run for a gameweek
that already has an authoritative row flips that prior row's
``is_authoritative`` to ``False`` first, then inserts a new row with
``is_authoritative=True`` -- never updates the prior row's own content.
``user_actual_decision_json`` is the ONE deliberate exception: it stays NULL
here, always, and is only ever filled in later by a future layer's
GAMEWEEK_REVIEW job (Milestone 7 Layer 4, not built yet) recording what the
user actually did versus what was recommended -- this layer never reads or
writes it beyond leaving it NULL at insert time.

``optimization_run_id`` covers BOTH ``SQUAD_OPTIMIZATION`` and
``LINEUP_OPTIMIZATION`` (both write to ``optimization_runs``, distinguished
only by ``recommendation_type`` -- see that model's own docstring) -- there is
deliberately only ONE FK slot for both, not two: this layer resolves whichever
of the two is more RECENT for the target gameweek, since a fresher
lineup-only chain-through is just as current a "what to play" answer as a
fresh joint squad+XI solve, and a caller who needs to know which kind it was
can always follow the FK and read ``OptimizationRun.recommendation_type``
itself.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean as SABoolean
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func, text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.domain.ingestion import EndpointHealth
from packages.domain.learning import RecommendationConfidence


class RecommendationAudit(Base):
    __tablename__ = "recommendation_audits"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("jobs.id"))
    season_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("seasons.id"), nullable=False
    )
    target_gameweek_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("gameweeks.id"), nullable=False
    )
    target_fpl_event_id: Mapped[int] = mapped_column(Integer, nullable=False)
    is_authoritative: Mapped[bool] = mapped_column(SABoolean, nullable=False, default=False)
    optimization_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("optimization_runs.id")
    )
    transfer_optimization_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("transfer_optimization_runs.id")
    )
    h2h_simulation_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("h2h_simulation_runs.id")
    )
    chip_optimization_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("chip_optimization_runs.id")
    )
    champion_model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("model_versions.id")
    )
    fpl_api_health_at_generation: Mapped[EndpointHealth | None] = mapped_column(
        SAEnum(
            EndpointHealth,
            name="recommendation_audit_fpl_api_health",
            native_enum=False,
            validate_strings=True,
        )
    )
    application_version: Mapped[str | None] = mapped_column(String(50))
    confidence: Mapped[RecommendationConfidence] = mapped_column(
        SAEnum(
            RecommendationConfidence,
            name="recommendation_audit_confidence",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
    )
    confidence_drivers_json: Mapped[list | None] = mapped_column(JSONB)
    assumptions_json: Mapped[list | None] = mapped_column(JSONB)
    known_risks_json: Mapped[list | None] = mapped_column(JSONB)
    # The ONE column ever updated post-insert -- see module docstring. Always
    # NULL when this layer inserts a row; only a future GAMEWEEK_REVIEW job
    # (Milestone 7 Layer 4) ever populates it.
    user_actual_decision_json: Mapped[dict | None] = mapped_column(JSONB)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        # Mirrors manager_squad_snapshots' own ix_manager_squad_snapshots_manager_is_current
        # partial-unique-index technique exactly (same reasoning: at most one CURRENT/
        # authoritative row per logical key at any time, enforced by the database itself,
        # not just by application-level discipline).
        Index(
            "ix_recommendation_audits_gameweek_authoritative",
            "season_id",
            "target_gameweek_id",
            unique=True,
            postgresql_where=text("is_authoritative"),
        ),
        Index("ix_recommendation_audits_season_gameweek", "season_id", "target_gameweek_id"),
    )
