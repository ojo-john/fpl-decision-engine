"""The ``counterfactual_results`` table: one row per computed baseline-vs-alternative comparison,
per ``recommendation_outcomes`` row.

Milestone 7 Layer 2 (Recursive Learning Loop: Outcome Ingestion/Counterfactual
Engine). Insert-only CHILD of one ``recommendation_outcomes`` row (``ON DELETE
CASCADE``, mirrors ``TransferOptimizationRunScenario``/``H2HStrategyCandidate``/
``ChipWindowCandidate``'s own run-child convention) -- but see
``RecommendationOutcome``'s own docstring for why a RERUN of
``ingest_outcome_for_audit`` explicitly deletes and re-inserts this table's
rows for that outcome (upsert-in-place at the PARENT's granularity), rather
than ever updating a ``counterfactual_results`` row itself in place.

Between 0 and 8 rows per ``recommendation_outcomes`` row -- not every
``CounterfactualComparisonType`` is computable for every audit (e.g. no
``OVERALL_VS_H2H_ADJUSTED``/``CONSERVATIVE_VS_AGGRESSIVE`` at all when the
audit's own ``h2h_simulation_run_id`` was NULL) -- see
``app.learning.outcome_ingestion`` for the exact per-type availability rules,
and ``app.learning.counterfactuals`` for the pure computation + the
per-comparison-type ``is_value_adding`` directionality convention.

``baseline_points``/``alternative_points``/``delta`` are always REALIZED
(actual, now-known) points, never the ex-ante projected/simulated estimates
the underlying component runs persisted -- that is the entire point of this
table: re-expressing an already-made recommendation's own comparison using
real outcomes once they exist, not recomputing the original decision.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean as SABoolean
from sqlalchemy import DateTime, ForeignKey, Index, Numeric, UniqueConstraint, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.domain.learning import CounterfactualComparisonType


class CounterfactualResult(Base):
    __tablename__ = "counterfactual_results"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    recommendation_outcome_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("recommendation_outcomes.id", ondelete="CASCADE"), nullable=False
    )
    comparison_type: Mapped[CounterfactualComparisonType] = mapped_column(
        SAEnum(
            CounterfactualComparisonType,
            name="counterfactual_result_comparison_type",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
    )
    baseline_points: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    alternative_points: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    delta: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    is_value_adding: Mapped[bool] = mapped_column(SABoolean, nullable=False)
    baseline_description_json: Mapped[dict | None] = mapped_column(JSONB)
    alternative_description_json: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "recommendation_outcome_id", "comparison_type", name="uq_counterfactual_results_outcome_type"
        ),
        Index("ix_counterfactual_results_outcome_id", "recommendation_outcome_id"),
    )
