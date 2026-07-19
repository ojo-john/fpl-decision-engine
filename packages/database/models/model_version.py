"""The ``model_versions`` table: one row per trained/evaluated candidate, per training run.

Populated by Milestone 3 Layer 3's ``MODEL_TRAINING`` job (see
``app/jobs/handlers/model_training.py``). Every candidate evaluated during a
training run — the 4 pure-lookup baselines (see ``app/modeling/baselines.py``)
and the 3 real scikit-learn tree regressors (see ``app/modeling/candidates.py``)
— gets its own row here, so all 7 participate in ``model_metrics``/champion
comparison on equal footing.

Each ``MODEL_TRAINING`` run is its own fresh audit snapshot, never an
upsert-in-place: re-running training with identical inputs still inserts a
brand new set of rows (mirrors ``ingestion_runs``' own "each run is a
distinct, recordable event" convention — see that model's docstring — rather
than the idempotent-upsert convention used for ``player_gameweek_stats``/
``player_features``). This is deliberate: retraining is itself a new event
worth recording even when the inputs are unchanged, e.g. to detect training
non-determinism across runs.

``artifact_uri`` is ``NULL`` for the 4 baseline families — they are pure
lookups/formulas over already-materialized ``player_features`` columns (or,
for ``BASELINE_RULES_EP_NEXT``, one extra ``player_gameweek_stats`` lookup),
never a fitted scikit-learn object, so there is nothing to serialize.

``lifecycle_state`` (Milestone 7 Layer 5: Challenger Training/Shadow-Mode
Evaluation/Promotion Gates/Rollback) tracks where this row sits in the
challenger/promotion lifecycle — see ``packages.domain.learning
.ModelLifecycleState`` for the full state set. Kept IN SYNC with
``is_champion`` as an ENFORCED invariant (``is_champion == (lifecycle_state ==
CHAMPION)``), but deliberately NOT via a DB-level CHECK constraint — see that
enum's own docstring for exactly why (some pre-existing tests seed
``is_champion`` directly, bypassing invariant-enforcing code on purpose).
Every real code path that flips ``is_champion``
(``app.jobs.handlers.model_training``, ``app.modeling.promotion``) also sets
``lifecycle_state`` consistently. Defaults to ``CANDIDATE`` — every freshly
inserted row starts there regardless of whether it goes on to become a
champion/shadow challenger.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean as SABoolean
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.domain.learning import ModelLifecycleState
from packages.domain.modeling import ModelFamily


class ModelVersion(Base):
    __tablename__ = "model_versions"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_name: Mapped[str] = mapped_column(String, nullable=False)
    model_family: Mapped[ModelFamily] = mapped_column(
        SAEnum(ModelFamily, name="model_version_model_family", native_enum=False, validate_strings=True),
        nullable=False,
    )
    feature_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # Kept as a plain string (not an enum) for forward-compat: a later
    # milestone may introduce other target variables (e.g. a staged
    # minutes/events pipeline) without a schema migration here.
    target_variable: Mapped[str] = mapped_column(String(50), nullable=False)
    training_seasons_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    hyperparameters_json: Mapped[dict | None] = mapped_column(JSONB)
    # NULL for the 4 baseline families -- no artifact needed, pure lookups/formulas.
    artifact_uri: Mapped[str | None] = mapped_column(String)
    artifact_checksum_sha256: Mapped[str | None] = mapped_column(String(64))
    is_champion: Mapped[bool] = mapped_column(SABoolean, nullable=False, default=False)
    lifecycle_state: Mapped[ModelLifecycleState] = mapped_column(
        SAEnum(
            ModelLifecycleState,
            name="model_version_lifecycle_state",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
        default=ModelLifecycleState.CANDIDATE,
    )
    trained_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    training_job_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("jobs.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_model_versions_feature_version", "feature_version"),
        Index("ix_model_versions_is_champion", "is_champion"),
        Index("ix_model_versions_lifecycle_state", "lifecycle_state"),
    )
