"""The ``learning_signals`` table: one row per rolling-window computation, insert-only.

Milestone 7 Layer 3 (Recursive Learning Loop: Failure Attribution/Rolling
Learning Signals). Populated by the ``compute_rolling_*``/
``compute_failure_reason_concentration`` functions in ``app.learning.signals``
-- see that module's docstring for the exact window/threshold/sustained-
breach logic per ``LearningSignalType``.

Never upserted -- each computation is its own recordable event, matching
``ingestion_runs``'/``model_versions``'s own "a fresh run is a distinct,
recordable event" convention (see those models' own docstrings), NOT
``rival_profiles``'s "one current row, upserted in place" convention: a
signal computed today and a signal computed for the same
``(season_id, as_of_gameweek_id, signal_type)`` next week after more data
has landed are both worth keeping, not just the latest.

``window_size_gameweeks`` is the ACTUAL window used for this computation, not
necessarily the caller's requested default -- e.g. fewer gameweeks of history
than requested, or (for ``ROLLING_CHIP_RECOMMENDATION_VALUE``) the full
season-to-date span rather than a small fixed N (chips are rare -- see that
function's own docstring for why a short rolling window would be meaningless
for it).

``threshold_breached`` uses a SUSTAINED-window bar, not a single-observation
bar, for ``ROLLING_PROJECTION_MAE``/``ROLLING_H2H_WIN_PROBABILITY_CALIBRATION``
(a single bad gameweek must never flip this to ``True`` -- see
``app.learning.signals`` for the exact "N of the last M gameweeks" logic).
It is ALWAYS ``False`` for ``ROLLING_CHIP_RECOMMENDATION_VALUE`` (explicitly
informational only, never a retraining-decision signal -- see that function's
own docstring for why).

``threshold_config_snapshot_json`` records the actual threshold value(s) used
for THIS computation (e.g. ``{"degradation_threshold": 1.25,
"min_consecutive_degraded_gameweeks": 3, "baseline_mae": 1.8432}``) so a
later threshold-tuning change doesn't retroactively make an old
``threshold_breached`` value impossible to explain.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean as SABoolean
from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.domain.learning import LearningSignalType


class LearningSignal(Base):
    __tablename__ = "learning_signals"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    season_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("seasons.id"), nullable=False
    )
    as_of_gameweek_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("gameweeks.id"), nullable=False
    )
    signal_type: Mapped[LearningSignalType] = mapped_column(
        SAEnum(
            LearningSignalType,
            name="learning_signal_signal_type",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
    )
    window_size_gameweeks: Mapped[int] = mapped_column(Integer, nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    threshold_breached: Mapped[bool] = mapped_column(SABoolean, nullable=False)
    threshold_config_snapshot_json: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_learning_signals_season_signal_type", "season_id", "signal_type"),
        Index("ix_learning_signals_as_of_gameweek_id", "as_of_gameweek_id"),
    )
