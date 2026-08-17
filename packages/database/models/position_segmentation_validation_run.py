"""The ``position_segmentation_validation_runs`` table: one row per POSITION_SEGMENTATION_VALIDATION run.

Closes a gap ``app.modeling.promotion``'s own module docstring previously named explicitly:
"there is no per-position actuals breakdown computed anywhere in this codebase yet." This table is
where that breakdown now actually gets computed and persisted -- see
``app.modeling.position_segmentation`` for the real evaluation harness (walk-forward folds, reusing
``app.modeling.walk_forward``'s exact fold-building) and
``app.jobs.handlers.position_segmentation_validation`` for the job wrapper.

Deliberately a NEW, separate table, never touching ``model_versions``/``is_champion``: this is a
read-only diagnostic comparing two TRAINING ARCHITECTURES (one global model across all positions vs.
four position-specific models), not a trained artifact competing to become the live champion. The
position-specific models trained during this evaluation are fit fresh, in memory, purely to compute
this run's MAE numbers -- never serialized, never given their own ``model_versions`` rows. Whether to
ever build a position-specific champion for real is a separate, later decision this table's real
numbers can inform, not something this job performs itself.

"Build fully in memory, insert once" persistence discipline, mirroring ``backtest_runs`` (see that
model's own docstring for the full rationale) -- only ``SUCCEEDED`` is ever actually written here
(``PositionSegmentationValidationRunStatus``'s own docstring).
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
from packages.domain.learning import PositionSegmentationValidationRunStatus


class PositionSegmentationValidationRun(Base):
    __tablename__ = "position_segmentation_validation_runs"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("jobs.id"))
    feature_version: Mapped[int] = mapped_column(Integer, nullable=False)
    run_status: Mapped[PositionSegmentationValidationRunStatus] = mapped_column(
        SAEnum(
            PositionSegmentationValidationRunStatus,
            name="position_segmentation_validation_run_status",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
        default=PositionSegmentationValidationRunStatus.SUCCEEDED,
    )
    usable_seasons_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    fold_count: Mapped[int] = mapped_column(Integer, nullable=False)
    degenerate_single_season: Mapped[bool] = mapped_column(SABoolean, nullable=False)
    # {"GKP": {"global_mae": ..., "position_specific_mae": ..., "mae_gap_pct": ..., "n_observations":
    # ...}, ...} -- mae_gap_pct = (global_mae - position_specific_mae) / global_mae: positive means the
    # position-specific model beat the global model for that position (a real gap the single-model
    # architecture doesn't capture); negative means the global model already handles that position
    # fine or better. See app.modeling.position_segmentation's own docstring for the exact formula.
    per_position_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    overall_global_mae: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    overall_position_specific_mae: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    n_observations: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_position_segmentation_validation_runs_job_id", "job_id"),)
