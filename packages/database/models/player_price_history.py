"""The ``player_price_history`` table: one insert-only row per OBSERVED ``now_cost`` change.

``app.ingestion.normalization.upsert_players`` appends a row here only when a
player's incoming ``now_cost`` differs from what was already stored for that
``(season_id, fpl_element_id)`` -- never once per ingestion run. This mirrors
``learning_signals``'/``ingestion_runs``'s own "a real, distinct event is
worth keeping, not just the latest value" convention, not
``rival_profiles``'s "one current row, upserted in place" one: a price
history with only the latest value would defeat the whole point of tracking
history.

``player_id`` references the CURRENT season's ``players`` row (price history
is season-scoped, matching how ``now_cost`` itself is a per-season concept —
a player's price resets each new FPL season). ``old_cost``/``new_cost`` are
both in the same tenths-of-a-million units ``players.now_cost`` already
uses (e.g. ``150`` = £15.0m) — no unit conversion happens here, this table
just observes what changed.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base


class PlayerPriceHistory(Base):
    __tablename__ = "player_price_history"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    player_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("players.id"), nullable=False
    )
    season_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("seasons.id"), nullable=False
    )
    old_cost: Mapped[int] = mapped_column(Integer, nullable=False)
    new_cost: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_player_price_history_player_id", "player_id"),
        Index("ix_player_price_history_season_id", "season_id"),
    )
