"""The ``failure_attributions`` table: zero or more mechanically-classified causes per
``recommendation_outcomes`` row.

Milestone 7 Layer 3 (Recursive Learning Loop: Failure Attribution/Rolling
Learning Signals). Insert-only CHILD of one ``recommendation_outcomes`` row
(``ON DELETE CASCADE``, mirrors ``CounterfactualResult``'s own run-child
convention) -- computed by ``app.learning.attribution.classify_failure_attributions``
from that outcome's own ``counterfactual_results`` children plus the original
``recommendation_audits`` row's generation-time context (see that module's
docstring for the full detection logic and materiality thresholds per
``FailureReasonCode``).

Unlike ``CounterfactualResult`` (exactly one row per ``CounterfactualComparisonType``,
enforced by a unique constraint), a ``recommendation_outcome_id`` here can
have MULTIPLE rows with the SAME ``reason_code`` in principle (e.g. a genuinely
distinct piece of evidence could independently trigger the same code twice) --
no uniqueness constraint is enforced on ``(recommendation_outcome_id,
reason_code)``, a deliberate judgment call favoring flexibility over a
constraint that would need to be relaxed the first time two independent
triggers legitimately produce the same code.

Exactly one row with ``reason_code=RECOMMENDATION_WAS_SOUND`` is written when
NO material miss was found at all (see that enum member's own docstring) --
this is how "checked, was fine" is distinguished from "never checked" (zero
rows).

``severity`` is a continuous materiality score (typically the ``|delta|``
points that triggered the classification, or the projection error in points,
or the rival-captain-prediction's own top probability) -- NOT a fixed enum,
since "how bad" is genuinely continuous and a coarse bucket would throw away
real information a later layer might want (e.g. ranking the worst misses).
Nullable because some codes have no natural single numeric magnitude (e.g.
``FPL_API_DEGRADED_OR_BLOCKED``, ``RECOMMENDATION_WAS_SOUND``).

``evidence_json`` is a small, code-specific dict recording exactly what
triggered the classification (e.g. ``{"counterfactual_comparison_type":
"chip_vs_hold", "delta": -8.5}`` or ``{"fpl_api_health_at_generation":
"blocked"}``) -- always present for a real classification, ``None`` only for
degenerate cases.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.domain.learning import FailureCategory, FailureReasonCode


class FailureAttribution(Base):
    __tablename__ = "failure_attributions"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    recommendation_outcome_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("recommendation_outcomes.id", ondelete="CASCADE"), nullable=False
    )
    reason_code: Mapped[FailureReasonCode] = mapped_column(
        SAEnum(
            FailureReasonCode,
            name="failure_attribution_reason_code",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
    )
    category: Mapped[FailureCategory] = mapped_column(
        SAEnum(
            FailureCategory,
            name="failure_attribution_category",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
    )
    severity: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    evidence_json: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_failure_attributions_outcome_id", "recommendation_outcome_id"),)
