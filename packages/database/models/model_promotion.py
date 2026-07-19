"""The ``model_promotions`` table: one insert-only row per promotion DECISION.

Milestone 7 Layer 5 (Recursive Learning Loop: Challenger Training/Shadow-Mode
Evaluation/Promotion Gates/Rollback). Populated by
``app.modeling.promotion.decide_and_record_promotion``/
``rollback_to_previous_champion`` -- every gate evaluation this module runs
gets its own row here, for ALL FOUR verdicts
(``PROMOTED``/``REJECTED``/``RETAINED_INCONCLUSIVE``/``ROLLED_BACK``) -- this
is the audit trail of every promotion decision this system has ever made
(including inconclusive ones), not just the ones that changed which model is
live. A ``RETAINED_INCONCLUSIVE`` row has no lifecycle-state side effects
(the challenger it names simply stays ``SHADOW_CHALLENGER``) but is still
worth keeping: it is direct evidence of when this challenger was last
checked and why nothing changed.

``challenger_model_version_id`` names the model version this row is ABOUT --
for a ``PROMOTED``/``REJECTED`` row that is genuinely a shadow challenger; for
a ``ROLLED_BACK`` row it is instead the ``RETIRED_CHAMPION`` being reinstated
(there is no separate "target" FK -- reusing this column keeps the schema
from growing a second, almost-always-null FK for the one row type that needs
it differently; see ``rollback_to_previous_champion``'s own docstring).

``previous_champion_model_version_id`` is ``NULL`` only for the very first
bootstrap promotion (see ``app.jobs.handlers.model_training`` -- that case
never goes through this module at all, since a bootstrap promotion happens
when NO champion exists yet, so this column is actually always populated for
every row THIS module itself creates; kept nullable for schema robustness
and to honestly describe the bootstrap case if it is ever recorded here).

``gate_results_json`` is ``PromotionGateResult.gate_checks`` verbatim (one
entry per named gate, pass/fail) -- ``NULL`` for a ``ROLLED_BACK`` row, which
does not go through the gate at all (a rollback is an operator decision, not
a gate-evaluated promotion).
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.domain.learning import PromotionVerdict


class ModelPromotion(Base):
    __tablename__ = "model_promotions"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    challenger_model_version_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("model_versions.id"), nullable=False
    )
    previous_champion_model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("model_versions.id")
    )
    verdict: Mapped[PromotionVerdict] = mapped_column(
        SAEnum(PromotionVerdict, name="model_promotion_verdict", native_enum=False, validate_strings=True),
        nullable=False,
    )
    gate_results_json: Mapped[dict | None] = mapped_column(JSONB)
    evaluation_window_gameweeks: Mapped[int | None] = mapped_column(Integer)
    n_shadow_evaluations: Mapped[int | None] = mapped_column(Integer)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_by_job_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("jobs.id"))
    report_markdown: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_model_promotions_challenger_id", "challenger_model_version_id"),
        Index("ix_model_promotions_decided_at", "decided_at"),
    )
