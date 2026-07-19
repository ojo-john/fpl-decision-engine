"""The ``backtest_runs`` table: one row per BACKTEST job run.

Milestone 7 Layer 6 (Recursive Learning Loop: BACKTEST job + the
GAMEWEEK_REVIEW -> MODEL_TRAINING retraining-trigger wiring). Populated by
``app.jobs.handlers.backtest.BacktestJobRunner``, deliberately scoped narrowly
per the plan's own scope cut: this is walk-forward *prediction-accuracy*
backtesting of one already-trained ``model_versions`` row, NOT a historical
decision-replay engine.

Answers a genuinely different question than ``MODEL_TRAINING``'s own
walk-forward validation does: "how would THIS ALREADY-TRAINED model's own
walk-forward MAE look if evaluated fresh today, against whatever historical
seasons are now available (which may have grown since it was originally
trained)?" -- the model itself is never refit; each of
``app.modeling.walk_forward.build_expanding_window_folds``'s folds is scored
by re-running the SAME already-fitted artifact (or, for a baseline family,
the same pure lookup/formula) against that fold's validation-season rows,
using the exact same metric functions ``app.modeling.metrics``/
``app.modeling.champion`` provide for the original training run.

Deliberately a NEW, separate table rather than reusing ``model_metrics``:
that table's own ``UniqueConstraint("model_version_id", "fold_index")`` is
scoped to ONE ``MODEL_TRAINING`` run's own fold-level results for that
model version -- a backtest run's own fold-level results would either
collide with that constraint (same model_version_id, same fold_index values
recomputed against a possibly-different, grown dataset) or require awkwardly
overloading ``fold_index`` with a second meaning, silently conflating a
BACKTEST run's own output with the original training run's own audit trail.
A dedicated table keeps the two completely unambiguous and never mutates a
single ``model_metrics`` row.

Persistence discipline: "build fully in memory, insert once" (mirrors
``RECOMMENDATION_GENERATION``'s own precedent, not the
create-early-RUNNING-row discipline ``GAMEWEEK_REVIEW``/the optimizer jobs
use) -- BACKTEST is pure computation over already-persisted, read-only data
with no other side-effecting steps to sequence, so there is nothing gained by
creating a row before the result is fully known; see
``packages.domain.learning.BacktestRunStatus``'s own docstring for why only
``SUCCEEDED`` is ever actually written here. Each BACKTEST run is its OWN
fresh row, never upserted -- mirrors ``model_versions``' own "each run is a
distinct, recordable event" convention, since re-running a backtest (e.g.
after more seasons have been ingested) is itself worth recording as a new
data point, not silently overwriting the previous one.

``fold_metrics_json`` is the full per-fold metrics list (mirrors
``model_metrics``' own per-fold columns, just JSON-shaped here rather than
one row per fold, since there is no other table that needs to join against
an individual backtest fold row the way ``model_challenger_evaluations``
needs to join against individual gameweeks). The ``aggregate_*`` columns are
the same simple-mean-across-folds aggregation
``app.modeling.champion.aggregate_mae``/``aggregate_spearman`` already use
for the original training run, applied here to this backtest's own folds.
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
from packages.domain.learning import BacktestRunStatus


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("jobs.id"))
    model_version_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("model_versions.id"), nullable=False
    )
    feature_version: Mapped[int] = mapped_column(Integer, nullable=False)
    run_status: Mapped[BacktestRunStatus] = mapped_column(
        SAEnum(BacktestRunStatus, name="backtest_run_status", native_enum=False, validate_strings=True),
        nullable=False,
        default=BacktestRunStatus.SUCCEEDED,
    )
    usable_seasons_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    fold_count: Mapped[int] = mapped_column(Integer, nullable=False)
    degenerate_single_season: Mapped[bool] = mapped_column(SABoolean, nullable=False)
    fold_metrics_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    # NULL only in the (should-not-happen-in-a-SUCCEEDED-row) case no fold produced a single
    # successfully-scored observation -- that case fails the job outright instead (see
    # app.jobs.handlers.backtest), so these are effectively always populated for a real row.
    aggregate_mae: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    aggregate_rmse: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    aggregate_mean_bias: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    aggregate_spearman: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    aggregate_top_20_capture_rate: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    n_observations: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_backtest_runs_model_version_id", "model_version_id"),
        Index("ix_backtest_runs_job_id", "job_id"),
    )
