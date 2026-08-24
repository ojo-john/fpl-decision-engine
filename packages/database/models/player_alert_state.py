"""The ``player_alert_states`` table: one row per player, upserted in place.

Milestone 9 Layer 4 (proactive alerting). Unlike ``player_price_signal_observations`` (insert-only
-- every run's observation is itself worth keeping for calibration, see that model's own
docstring), this table only ever needs the LATEST known status/momentum to diff the next
``DATA_INGESTION``-driven check against -- same "one current row, upserted in place" convention as
``teams``/``players``/``rival_profiles``, not an audit trail.

``player_id`` is unique: ``Player`` rows are already season-scoped (a new row per season, see
``Player.season_id``), so there is no cross-season collision risk the way ``DEADLINE_REMINDER``'s
own ``fpl_event_id`` dedup had to guard against (FPL's small integer gameweek numbers reset every
season; a UUID primary key does not).
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base


class PlayerAlertState(Base):
    __tablename__ = "player_alert_states"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    player_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("players.id"), nullable=False, unique=True
    )
    # Mirrors Player.status's own column shape (packages/database/models/player.py) -- the last
    # value app.jobs.handlers.player_alert_check.PlayerAlertCheckJobRunner observed, diffed against
    # on the next check.
    status: Mapped[str | None] = mapped_column(String(5))
    chance_of_playing_next_round: Mapped[int | None] = mapped_column(Integer)
    # app.players.price_signal.PriceMomentum's own string value ("likely_rise"/"likely_fall"/
    # "stable"/"unknown") -- stored as a plain string, not native_enum=False SAEnum, since this
    # column is read back only for equality comparison against a freshly-classified value, never
    # queried/filtered on directly.
    price_momentum: Mapped[str | None] = mapped_column(String(20))
    last_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
