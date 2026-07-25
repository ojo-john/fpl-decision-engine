"""The ``player_price_signal_observations`` table: one row per player, per ``DATA_INGESTION`` run.

Insert-only, same "a real, distinct event is worth keeping, not just the
latest value" convention as ``player_price_history`` -- NOT
``rival_profiles``'s "one current row, upserted in place" one. This table
exists specifically to give ``app.players.price_signal_calibration`` a
historical record to calibrate against: ``app.players.price_signal``'s
momentum heuristic reads ``players.transfers_in_event``/
``transfers_out_event``/``selected_by_percent`` (see migration 0028), but
those columns are OVERWRITTEN every ``DATA_INGESTION`` run -- there was
previously no way to know what the signal looked like at any point in the
past, so a price change observed in ``player_price_history`` could never be
paired back against "what did the signal say right before this happened".

``app.ingestion.normalization.record_price_signal_observations`` appends one
row per current-season player at the end of every successful ``DATA_INGESTION``
run, mirroring how ``upsert_players`` appends to ``player_price_history`` --
except this table gets a row every run regardless of whether anything
changed, since the ABSENCE of momentum (a quiet gameweek) is just as
necessary for calibration as a real spike (see
``app.players.price_signal_calibration.build_calibration_samples``, which
needs a "before" and "after" observation to determine whether a price change
happened in between).

``total_players`` is duplicated onto each row (rather than joined from
``seasons`` at calibration time) so a row is a fully self-contained snapshot
of the inputs ``classify_price_momentum`` had available at that instant --
``seasons.total_players`` itself changes over time, so joining live would
silently rewrite history.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base


class PlayerPriceSignalObservation(Base):
    __tablename__ = "player_price_signal_observations"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    player_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("players.id"), nullable=False
    )
    season_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("seasons.id"), nullable=False
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    transfers_in_event: Mapped[int | None] = mapped_column(Integer)
    transfers_out_event: Mapped[int | None] = mapped_column(Integer)
    selected_by_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 1))
    total_players: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_player_price_signal_observations_player_id_observed_at", "player_id", "observed_at"),
        Index("ix_player_price_signal_observations_season_id", "season_id"),
    )
