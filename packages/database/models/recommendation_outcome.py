"""The ``recommendation_outcomes`` table: one row per ``recommendation_audits`` row whose target
gameweek has since finished.

Milestone 7 Layer 2 (Recursive Learning Loop: Outcome Ingestion/Counterfactual
Engine). Populated by ``app.learning.outcome_ingestion.ingest_outcome_for_audit``,
which resolves the real outcome (what actually happened) for one already-generated
recommendation summary once the FPL API confirms its ``target_gameweek_id`` is
``finished=True`` -- never computed against a still-in-progress gameweek (see
that module's own ``GameweekNotFinishedError``).

Unlike ``recommendation_audits``/``manager_squad_snapshots`` (insert-only,
audit-snapshot semantics -- a fresh generation is its own recordable event),
this table converges to ONE true value per audit, the same way ``h2h_matches``
converges to one resolved result as a gameweek plays out (see that model's own
docstring) -- a finished gameweek's outcome is a fact, not a sequence of
observations worth preserving individually. So ``ingest_outcome_for_audit`` is
idempotent via UPSERT-IN-PLACE: a second call for the same
``recommendation_audit_id`` UPDATES this row's own scalar fields and REPLACES
its ``counterfactual_results`` children wholesale (delete then re-insert),
rather than either refusing outright or accumulating a second row. This is a
deliberate judgment call (the unique constraint on ``recommendation_audit_id``
would just as easily support "refuse a second call") -- upsert was chosen
because a rerun is exactly the case where newly-corrected
``player_gameweek_stats``/``h2h_matches`` data (e.g. a late bonus-points
correction) should legitimately update a previously-computed outcome, not be
silently ignored.

``actual_xi_points``/``actual_captain_points``/``actual_bench_points`` describe
what the USER'S ACTUAL submitted squad scored (resolved from the post-gameweek
``MANAGER_SYNC`` ``manager_squad_snapshots`` row for this exact gameweek, via
``raw_picks_json`` -- see ``ManagerSquadSnapshot``'s own docstring for why this
IS the real "what did the user actually do" resolution, reused verbatim here,
not re-fetched). ``actual_captain_points`` already has the real captain
multiplier applied (2x, or 3x if Triple Captain was the ``active_chip`` that
gameweek) -- NOT the raw single-player score. These three fields are NULL
whenever no such actual snapshot can be resolved (e.g. the recommendation's own
manager squad snapshot was a ``MANUAL_ENTRY`` with no real ``fpl_manager_id``
behind it, or the user simply never synced after this gameweek finished) -- a
real, common gap, never fabricated.

``recommendation_net_gain`` is the ``TRANSFER_VS_NO_TRANSFER`` counterfactual's
own ``delta`` when that comparison was computable (the clearest "did the user
gain anything vs. doing nothing different" signal this system can compute),
NULL otherwise -- see ``app.learning.outcome_ingestion`` for exactly where this
is set. It is a convenience summary field, not an independent computation of
its own.

``h2h_result`` is a plain ``String(10)`` ("win"/"draw"/"loss"), not its own
enum -- a deliberate, documented judgment call: 3 fixed literal values used in
exactly one column don't earn a new enum type the way every OTHER string-typed
column in this codebase's models already accepts plain strings for a similarly
small closed set (e.g. ``ManagerSquadSnapshot.active_chip``,
``ChipRecommendation`` verdicts being the one deliberate exception because
``ChipVerdict`` already existed for a job's own return-type contract). Consistency
with those precedents was weighed more heavily here than enum-everywhere purity.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base


class RecommendationOutcome(Base):
    __tablename__ = "recommendation_outcomes"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    recommendation_audit_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("recommendation_audits.id"), nullable=False, unique=True
    )
    actual_xi_points: Mapped[int | None] = mapped_column(Integer)
    actual_captain_points: Mapped[int | None] = mapped_column(Integer)
    actual_bench_points: Mapped[int | None] = mapped_column(Integer)
    h2h_match_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("h2h_matches.id")
    )
    # Plain string, not an enum -- see module docstring.
    h2h_result: Mapped[str | None] = mapped_column(String(10))
    opponent_actual_squad_player_ids_json: Mapped[list | None] = mapped_column(JSONB)
    # Raw FPL element id, matching RivalGameweekSnapshot.captain_element_id's own convention.
    opponent_actual_captain_player_id: Mapped[int | None] = mapped_column(Integer)
    actual_transfers_made_json: Mapped[list | None] = mapped_column(JSONB)
    actual_hit_cost_taken: Mapped[int | None] = mapped_column(Integer)
    actual_chip_played: Mapped[str | None] = mapped_column(String(20))
    recommendation_net_gain: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    finalized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("recommendation_audit_id", name="uq_recommendation_outcomes_audit_id"),
    )
