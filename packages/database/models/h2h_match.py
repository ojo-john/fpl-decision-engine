"""The ``h2h_matches`` table: one row per H2H fixture within a league, upserted in place.

Upsert-in-place semantics keyed on ``fpl_match_id`` — NOT audit/insert-only
like ``rival_gameweek_snapshots``. A match record converges to one true
state as the gameweek plays out (``entry_*_points`` start ``NULL`` and
resolve once the gameweek finishes), the same convergence behavior as
``Gameweek.finished`` flipping from ``False`` to ``True`` in place — there is
exactly one current truth for "what happened in match X", not a sequence of
observations worth preserving individually.

``entry_1_win``/``entry_1_draw``/``entry_1_loss`` (and the ``entry_2_*``
mirrors) are the raw per-match H2H counters FPL returns (each is 0/1 for a
resolved match, all 0 before resolution) — stored verbatim, not
reinterpreted. FPL's raw response also includes redundant computed fields
(e.g. ``entry_1_total``, ``tiebreak``) not modeled as their own columns here;
those are preserved losslessly in ``raw_match_json`` for anything that needs
them later without requiring a schema change.

``winner`` is NOT stored verbatim, unlike the fields above — CORRECTED
UNDERSTANDING (2026-09-04, confirmed live against real production data):
FPL's own raw ``winner`` field is only ever populated for knockout-round
matches. A real, decisive regular-season result (e.g. 62-53, with
``entry_1_win=1``) still returns ``winner: null`` from FPL indefinitely,
across every resync after the gameweek finishes — this is not a sync delay,
it never resolves. ``app.jobs.handlers.league_sync._derive_winner`` trusts
FPL's own value when present (a knockout tie may encode tiebreak-resolved
info the win/draw/loss flags alone don't carry) and otherwise derives it
from ``entry_1_win``/``entry_2_win``, which FPL does reliably set once a
match has a real result. Note that no real application logic actually reads
this column today — ``app.learning.outcome_ingestion`` derives outcomes
directly from ``entry_1_points``/``entry_2_points`` instead — so this column
existing at all is for any future consumer that wants "who won" without
re-deriving it themselves.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean as SABoolean
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base


class H2HMatch(Base):
    __tablename__ = "h2h_matches"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    fpl_match_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    league_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("leagues.id"), nullable=False
    )
    event: Mapped[int] = mapped_column(Integer, nullable=False)  # gameweek number
    entry_1_id: Mapped[int] = mapped_column(Integer, nullable=False)  # raw FPL manager id
    entry_2_id: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_1_points: Mapped[int | None] = mapped_column(Integer)
    entry_2_points: Mapped[int | None] = mapped_column(Integer)
    entry_1_win: Mapped[int | None] = mapped_column(Integer)
    entry_1_draw: Mapped[int | None] = mapped_column(Integer)
    entry_1_loss: Mapped[int | None] = mapped_column(Integer)
    entry_2_win: Mapped[int | None] = mapped_column(Integer)
    entry_2_draw: Mapped[int | None] = mapped_column(Integer)
    entry_2_loss: Mapped[int | None] = mapped_column(Integer)
    # Derived, not raw verbatim -- see this module's own docstring for why.
    winner: Mapped[int | None] = mapped_column(Integer)  # FPL manager id; NULL = undecided or draw
    is_knockout: Mapped[bool] = mapped_column(SABoolean, nullable=False)
    knockout_name: Mapped[str | None] = mapped_column(String)
    is_bye: Mapped[bool] = mapped_column(SABoolean, nullable=False)
    seed_value: Mapped[int | None] = mapped_column(Integer)
    raw_match_json: Mapped[dict | None] = mapped_column(JSONB)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_h2h_matches_league_id", "league_id"),
        Index("ix_h2h_matches_league_event", "league_id", "event"),
    )
