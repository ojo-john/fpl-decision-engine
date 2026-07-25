"""The ``player_availability_risk_notes`` table: curated, judgment-based fitness/availability risk.

Distinct from FPL's own official injury/doubt signal (``players.chance_of_playing_next_round``,
``players.news``), which ``DATA_INGESTION`` already refreshes automatically every ~4 hours and
``app.optimization.projection.project_player`` already down-weights against (see
``_AVAILABILITY_SCALE_THRESHOLD``) -- that part needs no new table, it already works. This table
exists for the gap FPL's own data doesn't cover: a player who isn't officially listed as
injured/doubtful but has a real, sourced reason to expect reduced early-season involvement (e.g.
a player who went deep into an international tournament and returns to club training only days
before a season opener). This is NOT a background job that checks the news itself -- there is no
autonomous news-monitoring in this system, by design (this codebase only talks to the official FPL
API) -- these rows are added by a human, or an agent doing fresh research on request, same as any
other judgment call a manager makes about their own squad.

Each row is an independent note, NOT a single-active-row-per-player pattern like
``seasons.is_current``/``model_versions.is_champion``/``leagues.is_priority`` -- a player can have
zero, one, or (rarely) more than one note over time, and a note simply stops applying once its own
gameweek window (``valid_from_fpl_event_id``..``valid_until_fpl_event_id``) has passed, rather than
needing explicit deactivation. See ``app.optimization.projection._apply_availability_risk_notes``
for how these are looked up and applied, and ``apps.api.routers.players`` for the CRUD endpoints.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base


class PlayerAvailabilityRiskNote(Base):
    __tablename__ = "player_availability_risk_notes"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    player_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("players.id"), nullable=False
    )
    season_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("seasons.id"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # Projection multiplier in (0, 1] -- e.g. 0.65 means "reduce the projection to 65% of what it
    # would otherwise be" while this note is active. Never 0 (that's what the hard-exclude
    # statuses/i/s/u are for) -- this is a discount for reduced sharpness/rotation risk, not a
    # claim the player won't feature at all.
    severity: Mapped[Decimal] = mapped_column(Numeric(3, 2), nullable=False)
    # Gameweek window (FPL fpl_event_id, inclusive on both ends) this note applies to. Both
    # nullable: NULL valid_from means "from the start of the season"; NULL valid_until means "no
    # known end yet" (open-ended until someone clears it) -- in practice every note created so far
    # sets both, since the whole point is a note that naturally stops applying once the gameweek
    # window passes.
    valid_from_fpl_event_id: Mapped[int | None] = mapped_column(Integer)
    valid_until_fpl_event_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Free-text provenance, not a real FK -- this app has no user table, only a single shared
    # bearer token (see apps.api.auth), so "who added this" is whatever string the caller sends
    # (e.g. "manual", or a note about which research session found it).
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)

    __table_args__ = (
        Index("ix_player_availability_risk_notes_player_id", "player_id"),
        Index("ix_player_availability_risk_notes_season_id", "season_id"),
    )
