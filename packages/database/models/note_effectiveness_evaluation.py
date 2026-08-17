"""The ``note_effectiveness_evaluations`` table: per-note, per-outcome accuracy feedback.

Milestone 7 extension (Recursive Learning Loop, note-effectiveness tracking). Closes a gap
distinct from ``failure_attributions``/``counterfactual_results``: those mechanically classify
WHY a recommendation missed using data this codebase already has; this table instead answers a
narrower, note-specific question -- for a player who had an active ``player_availability_risk_notes``
or ``player_role_notes`` row applied to their projection for a reviewed gameweek, did applying
that note's own multiplier move the projection CLOSER to or FURTHER FROM what actually happened?

Every note is a one-off, unrepeatable judgment call (a specific injury, a specific role change)
-- there is no repeated historical sample to backtest any SINGLE note against, unlike the
cold-start blend's own half-life constant. What IS buildable, honestly, is a running record of
these individual (with-note error, without-note error) comparisons that, once enough real
gameweeks have been reviewed with real notes in play, lets a future session notice a PATTERN
across many one-off notes (e.g. "risk notes in the 0.60-0.75 band have tended to run too
conservative, based on N real outcomes") -- see ``app.learning.note_effectiveness`` for the
computation and ``apps.api.schemas.players.AvailabilityRiskNoteCreate``/``RoleNoteCreate`` for
the severity-selection rubric this feedback loop is meant to eventually inform. This table only
builds the tracking/reporting infrastructure -- no automated recalibration is wired up yet
(unlike ``price_signal_calibrations``' own real recalibration mechanism, the closest precedent
for what a MATURE version of this could eventually become, once real gameweek data exists to
support it).

Insert-only CHILD of one ``recommendation_outcomes`` row (``ON DELETE CASCADE``, mirrors
``CounterfactualResult``/``FailureAttribution``'s own run-child convention) -- a RERUN of
``app.learning.note_effectiveness.evaluate_note_effectiveness_for_outcome`` deletes and
re-persists this table's rows for that outcome, exactly like ``failure_attributions``' own
documented rerun behavior, rather than ever updating a row in place.

Zero, one, or (when a player has BOTH an active availability-risk note and an active role-boost
note stacked on the same projection) two rows per ``(recommendation_outcome_id, player_id)`` --
one row PER NOTE TYPE that was actually applied, since removing "just the risk note" and
removing "just the role note" are two independently meaningful counterfactual questions even
when both notes were active on the exact same projection at once. Enforced by a unique
constraint on ``(recommendation_outcome_id, player_id, note_type)``.

``projected_points_without_note`` is computed by dividing ``projected_points_with_note`` by
this row's own ``note_severity`` -- the note-application step
(``app.optimization.projection._apply_availability_risk_notes``/``_apply_role_boost_notes``) is
purely multiplicative, so dividing out exactly this note's own multiplier exactly reconstructs
what the SAME already-persisted projection would have been without it, using the real number the
recommendation was actually generated from -- not a fresh, independently-recomputed projection,
which could drift from what was actually decided on if underlying data changed since. See that
module's own docstring for the full derivation and why this is more forensically correct than a
recompute.

``moved_closer_to_actual`` is ``NULL`` only on the (rare, floating-point) case both errors come
out exactly equal -- a genuine "no difference" result, distinct from ``True``/``False``.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean as SABoolean
from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base


class NoteEffectivenessEvaluation(Base):
    __tablename__ = "note_effectiveness_evaluations"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    recommendation_outcome_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("recommendation_outcomes.id", ondelete="CASCADE"), nullable=False
    )
    player_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("players.id"), nullable=False
    )
    # "availability_risk" | "role_boost" -- plain str, not an enum, mirroring ActiveNoteOut.category's
    # own "nothing branches on the exact string" reasoning (this column is purely descriptive; the
    # which-note-type-applied fact is really carried by which of the two FK columns below is set).
    note_type: Mapped[str] = mapped_column(String(20), nullable=False)
    # Exactly one of these two is set, matching note_type -- ON DELETE SET NULL (not CASCADE, and not
    # a plain non-FK uuid column): player_availability_risk_notes/player_role_notes rows ARE
    # deletable (see apps.api.routers.players' own DELETE endpoints for both), and this evaluation
    # row's own severity/points/error columns already captured everything needed to stay meaningful
    # as a historical record even after the source note itself is later removed.
    availability_risk_note_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("player_availability_risk_notes.id", ondelete="SET NULL")
    )
    role_boost_note_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("player_role_notes.id", ondelete="SET NULL")
    )
    # The exact multiplier this note applied at generation time -- snapshotted here (not re-derived
    # from the note row, which could have since been deleted) since it is the one input this row's
    # own "divide out the note's own multiplier" derivation needs to remain valid forever.
    note_severity: Mapped[Decimal] = mapped_column(Numeric(4, 2), nullable=False)
    projected_points_with_note: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    projected_points_without_note: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    actual_points: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    error_with_note: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    error_without_note: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    moved_closer_to_actual: Mapped[bool | None] = mapped_column(SABoolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "recommendation_outcome_id",
            "player_id",
            "note_type",
            name="uq_note_effectiveness_evaluations_outcome_player_type",
        ),
        Index("ix_note_effectiveness_evaluations_outcome_id", "recommendation_outcome_id"),
        Index("ix_note_effectiveness_evaluations_player_id", "player_id"),
    )
