"""The ``model_metrics`` table: walk-forward validation results for a ``model_versions`` row.

Populated by Milestone 3 Layer 3's ``MODEL_TRAINING`` job. One row per
``(model_version, fold_index)`` — ``fold_index`` identifies a single
walk-forward season-level fold (see ``app/modeling/walk_forward.py``), and
``fold_index IS NULL`` marks the aggregate/overall row summarizing a
candidate's performance across every fold (see ``app/modeling/champion.py``
for the exact aggregation — currently a simple mean across folds).

Metric definitions live in ``app/modeling/metrics.py`` (pure functions over
``(y_true, y_pred)``) — this table only stores their results.
``spearman_rank_corr``/``top_20_capture_rate`` are nullable because they are
genuinely undefined in some small-sample edge cases (fewer than 2 distinct
rank values, or fewer than 20 observations, respectively) — never coerced to
a placeholder number.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base


class ModelMetric(Base):
    __tablename__ = "model_metrics"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_version_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("model_versions.id"), nullable=False
    )
    # NULL = the aggregate/overall row across all folds for this candidate.
    fold_index: Mapped[int | None] = mapped_column(Integer)
    validation_season_label: Mapped[str | None] = mapped_column(String(20))
    mae: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    rmse: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    mean_bias: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    # Undefined (not just "unknown") when fewer than 2 distinct rank values
    # exist in the validation fold -- never coerced to e.g. 0.
    spearman_rank_corr: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    top_20_capture_rate: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    calibration_bucket_json: Mapped[dict | None] = mapped_column(JSONB)
    n_observations: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("model_version_id", "fold_index", name="uq_model_metrics_model_version_fold_index"),
        Index("ix_model_metrics_model_version_id", "model_version_id"),
    )
