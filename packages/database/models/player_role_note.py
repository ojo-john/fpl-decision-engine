"""The ``player_role_notes`` table: curated, sourced set-piece/role-duty facts.

Closes a different gap than ``player_availability_risk_notes`` (see that model's own docstring):
who takes penalties/corners/free-kicks is a real signal that directly affects a player's expected
points (penalty takers score more goals, set-piece takers pick up more assists/bonus), but it is
NOT exposed anywhere in FPL's own ``bootstrap-static`` API, and it is not reliably inferable from
historical scoring alone -- a role can change hands mid-season, or a new signing can be handed
set-piece duty on arrival, before any of that shows up in their own scoring history. Concrete
motivating case: real Premier League Scout coverage citing Pascal Gross (Brighton) "now handling
penalty duties" and "main taker of corners and free-kicks" after a change in who takes them.

Deliberately a SEPARATE table/mechanism from ``player_availability_risk_notes``, not a reuse of
it, even though both are "a curated, sourced fact that adjusts a projection by a multiplier" --
two reasons this matters:

1. Direction: availability-risk notes are always a DISCOUNT (severity in (0, 1]); role notes are
   normally a BOOST (severity > 1.0, though nothing at the DB level hard-enforces that -- see the
   column's own comment). A player could plausibly have an active risk note (e.g. carrying a
   minor knock) AND an active role-boost note (recently handed penalties) AT THE SAME TIME -- both
   are real, independent facts and should both apply, multiplied together, not have one silently
   overwrite the other.
2. Multi-note tie-break direction: when several notes of the SAME kind are active for a player,
   ``get_active_availability_risk_note`` deliberately picks the MOST severe (lowest multiplier) --
   understating a real risk is worse than overstating it. Role notes invert that: the underlying
   fact ("who currently takes penalties") is usually unambiguous once properly sourced, so
   ``get_active_role_boost_note`` picks the HIGHEST severity (strongest confirmed boost) among
   several active role notes, on the theory that the most specific/strongest sourced claim is
   more likely to be the current, correct one than a weaker/older note that hasn't been cleared
   yet. Trying to cram both tie-break directions into one shared "pick a winner" function would be
   more confusing than keeping two small, clearly-named functions.

See ``app.optimization.projection.get_active_role_boost_note``/``_apply_role_boost_notes`` for how
these are looked up and applied (stacked with, not replacing, availability-risk notes), and
``apps.api.routers.players`` for the CRUD endpoints.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base


class PlayerRoleNote(Base):
    __tablename__ = "player_role_notes"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    player_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("players.id"), nullable=False
    )
    season_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("seasons.id"), nullable=False
    )
    # Free text, not an enum -- this is a curated fact a human/research agent writes, not a
    # closed set the codebase reasons about programmatically (nothing branches on the exact
    # string). Keep it short/structured-ish in practice, e.g. "penalties", "corners",
    # "free_kicks", "penalties_and_corners" -- but the column itself doesn't enforce that.
    role: Mapped[str] = mapped_column(String(100), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # Projection multiplier -- normally > 1.0 (a boost), e.g. 1.15 means "increase the projection
    # to 115% of what it would otherwise be" while this note is active. Deliberately NOT
    # constrained to a fixed range at the DB level (mirrors player_availability_risk_notes'
    # own plain Numeric(3,2) column) -- the multiplication in
    # app.optimization.projection._apply_role_boost_notes works identically regardless of
    # whether the value is above or below 1.0, so the mechanism itself stays generic even though
    # every note created in practice so far is a genuine boost.
    severity: Mapped[Decimal] = mapped_column(Numeric(4, 2), nullable=False)
    # Gameweek window (FPL fpl_event_id, inclusive on both ends). Unlike availability-risk notes
    # (which usually have a tight, short window -- an injury recovery period), a set-piece role
    # assignment tends to be a longer-lived fact with no known end date, so leaving
    # valid_until_fpl_event_id as NULL ("no known end yet, until someone clears it") will often
    # be the right default when creating a real note here.
    valid_from_fpl_event_id: Mapped[int | None] = mapped_column(Integer)
    valid_until_fpl_event_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Free-text provenance, not a real FK -- see player_availability_risk_notes' own identical
    # column for why (this app has no user table, only a single shared bearer token).
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)

    __table_args__ = (
        Index("ix_player_role_notes_player_id", "player_id"),
        Index("ix_player_role_notes_season_id", "season_id"),
    )
