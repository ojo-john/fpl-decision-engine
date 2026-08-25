"""The ``start_probability_models`` table: one row per ``START_PROBABILITY_MODEL_TRAINING`` run.

Populated by ``app/jobs/handlers/start_probability_training.py``. Deliberately a SEPARATE,
much simpler table from ``model_versions`` -- this is an auxiliary classifier (P(player starts
this gameweek)), never the main points-regression champion, and has no promotion/shadow-mode
lifecycle of its own (no ``lifecycle_state``, no challenger evaluation): see that job handler's
own module docstring for the full "why an auxiliary model, not a retrain" rationale.

``is_active`` mirrors ``model_versions.is_champion``'s shape (a boolean flag, application-enforced
"at most one true" invariant, not a DB-level CHECK constraint -- same precedent, see that column's
own docstring for why) but defaults to ``False`` and is NEVER flipped automatically by the training
job itself. Activating a freshly-trained row to actually affect live projections
(``app.optimization.projection.project_player_with_champion``, via
``app.optimization.start_probability_loader.load_active_start_probability_model``) is a
deliberate, separate decision this codebase leaves to a human reviewing this run's own real
backtest metrics (``aggregate_auc``/``aggregate_accuracy``/``aggregate_f1``/``aggregate_log_loss``
below) -- not something training success alone should trigger.

Each training run is its own fresh audit snapshot, never an upsert-in-place -- mirrors
``model_versions``' own "re-running training with identical inputs still inserts a brand new row"
convention (see that model's docstring for why).
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean as SABoolean
from sqlalchemy import DateTime, ForeignKey, Index, Integer, LargeBinary, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base


class StartProbabilityModel(Base):
    __tablename__ = "start_probability_models"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    feature_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # Plain string, not an enum -- mirrors model_versions.target_variable's own forward-compat
    # reasoning: describes the exact label definition used (see
    # app.modeling.start_probability.START_LABEL_MINUTES_THRESHOLD), so a later run using a
    # different threshold is self-describing without a schema migration.
    target_variable: Mapped[str] = mapped_column(String(100), nullable=False)
    training_seasons_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    hyperparameters_json: Mapped[dict | None] = mapped_column(JSONB)
    # NULL only if training somehow produced zero usable folds before persisting would occur --
    # in practice the job fails cleanly before ever inserting a row in that case (see the handler),
    # so this is realistically always populated once a row exists. Local-dev convenience only,
    # never authoritative for loading -- see model_versions.artifact_uri's own docstring for the
    # real production incident (2026-08-24) that explains why: a local filesystem path never
    # survives a real Render redeploy, which rebuilds the entire container filesystem from scratch.
    artifact_uri: Mapped[str | None] = mapped_column(String)
    # The sole authoritative artifact source app.optimization.start_probability_loader loads from.
    artifact_bytes: Mapped[bytes | None] = mapped_column(LargeBinary)
    artifact_checksum_sha256: Mapped[str | None] = mapped_column(String(64))

    # Real walk-forward-aggregated classification metrics (app.modeling.metrics' new
    # classification functions) -- see this table's own module docstring for why activation is a
    # separate, human-reviewed decision rather than automatic.
    aggregate_auc: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    aggregate_accuracy: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    aggregate_f1: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    aggregate_log_loss: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    n_observations: Mapped[int] = mapped_column(Integer, nullable=False)
    fold_metrics_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    usable_seasons_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    degenerate_single_season: Mapped[bool] = mapped_column(SABoolean, nullable=False, default=False)

    is_active: Mapped[bool] = mapped_column(SABoolean, nullable=False, default=False)

    trained_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    training_job_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("jobs.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_start_probability_models_feature_version", "feature_version"),
        Index("ix_start_probability_models_is_active", "is_active"),
    )
