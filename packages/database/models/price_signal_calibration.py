"""The ``price_signal_calibrations`` table: history of auto-tuned price-momentum thresholds.

Same "exactly one row has ``is_active=True`` at a time, application-enforced,
no DB constraint, unset every other row before setting the new one" invariant
this codebase already uses for ``seasons.is_current``/``model_versions
.is_champion``/``leagues.is_priority`` (see e.g. ``apps/api/routers/leagues
.py``'s ``UPDATE ... SET is_priority=False`` before the new row is flipped
on). Every recalibration INSERTS a new row rather than updating one in place
-- this preserves a full audit trail of how the thresholds drifted over the
season, mirroring ``model_versions``/``optimization_runs``'s own
"never overwrite, always append a new row and re-point the active flag"
philosophy.

Populated by ``app.jobs.handlers.gameweek_review.GameweekReviewJobRunner
._maybe_recalibrate_price_signal``, which runs after every finished
gameweek. If no row has ``is_active=True`` yet (the common case until enough
real price-change data has accumulated -- see
``app.players.price_signal_calibration.MIN_CALIBRATION_SAMPLE_SIZE``),
``app.players.price_signal``'s hardcoded ``DEFAULT_*`` constants are used
instead -- this table is an override, never a required dependency.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean as SABoolean
from sqlalchemy import DateTime, Integer, Numeric, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base


class PriceSignalCalibration(Base):
    __tablename__ = "price_signal_calibrations"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ratio_threshold: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False)
    absolute_floor: Mapped[int] = mapped_column(Integer, nullable=False)
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False)
    accuracy: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    # Exactly one row True at a time -- see module docstring.
    is_active: Mapped[bool] = mapped_column(SABoolean, nullable=False, default=False)
    calibrated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
