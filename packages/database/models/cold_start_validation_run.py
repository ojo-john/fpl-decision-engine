"""The ``cold_start_validation_runs`` table: one row per COLD_START_PROJECTION_VALIDATION job run.

Historical validation of ``app.optimization.projection._cold_start_projection``'s hand-written
blend formula -- the cold-start fallback EVERY pre-season squad-building decision this app exists
to support goes through, and which ``BACKTEST``/``MODEL_TRAINING``'s own walk-forward validation
never covers (they only ever evaluate a trained ``model_versions`` row; this formula is not one).
See ``app.modeling.cold_start_validation`` for the full leakage-safe evaluation harness and
``app.jobs.handlers.cold_start_projection_validation`` for the job itself.

Persists the real MAE/Spearman sweep results for all three of that formula's previously-unvalidated
constants: ``career_avg_early_season_points_prior_seasons``'s cross-season recency half-life, the
fixture-difficulty adjustment coefficient, and the ep_next/career_avg blend ratio -- one dedicated
JSONB column of sweep results per dimension, since they're evaluated at different granularities
(season-level, per-gameweek, season-level) and the ep_next-based sweep carries a caveat the other
two don't (see ``sweep_blend_ratio_candidates``'s own docstring).

Also persists a fourth, structurally different pair of results: the price-informed true-rookie
fallback's own blend-weight sweep (``n_true_rookie_observations``/``price_prior_sweep_json``/etc.)
and the LIVE price-comparable lookup table itself (``price_comparable_buckets_json``) --
``app.optimization.projection._price_informed_true_rookie_projection`` reads the most recent row's
``price_comparable_buckets_json`` directly, mirroring ``app.optimization.champion_loader
.load_current_champion``'s "load the most recent real artifact" precedent.

Same "build fully in memory, insert once" persistence discipline as ``backtest_runs`` (see that
model's own docstring for the full rationale) -- this is pure computation over already-persisted,
read-only historical data with no other side-effecting steps to sequence.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.domain.learning import ColdStartValidationRunStatus


class ColdStartValidationRun(Base):
    __tablename__ = "cold_start_validation_runs"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("jobs.id"))
    run_status: Mapped[ColdStartValidationRunStatus] = mapped_column(
        SAEnum(
            ColdStartValidationRunStatus,
            name="cold_start_validation_run_status",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
        default=ColdStartValidationRunStatus.SUCCEEDED,
    )
    early_season_gameweek_cutoff: Mapped[int] = mapped_column(Integer, nullable=False)
    # Documents the ep_next gap decision (see module docstring) -- what the half-life and
    # difficulty-coefficient sweeps evaluate ("career_avg_early_season_points_prior_seasons_only")
    # is NOT the full blended formula; only the blend-ratio sweep touches ep_next at all, and even
    # then only via a documented, imperfect historical proxy.
    component_evaluated: Mapped[str] = mapped_column(String(100), nullable=False)
    usable_seasons_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    seasons_evaluated_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    n_observations: Mapped[int] = mapped_column(Integer, nullable=False)

    half_life_sweep_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    default_half_life_seasons: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    default_half_life_mae: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    best_half_life_seasons: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    best_half_life_label: Mapped[str] = mapped_column(String(30), nullable=False)
    best_half_life_mae: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)

    difficulty_coefficient_sweep_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    default_difficulty_coefficient: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False)
    default_difficulty_coefficient_mae: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    # NULL only if literally zero early-season gameweeks had a resolvable fixture/difficulty rating
    # across every evaluable observation (see sweep_difficulty_coefficient_candidates) -- that
    # dimension is then skipped, not failed, mirroring the blend-ratio dimension's own convention.
    best_difficulty_coefficient: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    best_difficulty_coefficient_mae: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))

    blend_ratio_sweep_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    default_blend_career_avg_weight: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False)
    default_blend_mae: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    # NULL only if literally zero observations had a resolvable ep_next proxy at all (see
    # sweep_blend_ratio_candidates) -- "best" is then genuinely undefined, not failed.
    best_blend_career_avg_weight: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    best_blend_mae: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    blend_ratio_caveat: Mapped[str] = mapped_column(String(500), nullable=False)

    # --- Price-informed true-rookie fallback (see app.modeling.cold_start_validation's own
    # "A fourth, structurally different piece of validation" section and
    # app.optimization.projection._price_informed_true_rookie_projection). This population
    # (career_avg is None -- literally zero prior-season history) is DISJOINT from every
    # observation above (those all require non-empty prior_stats), so it gets its own,
    # independent sample size rather than reusing n_observations. Nullable: a schema-compatible
    # older row (or a future re-run that, for any reason, has zero true-rookie observations to
    # evaluate) still has a well-defined non-price-prior result set above.
    n_true_rookie_observations: Mapped[int | None] = mapped_column(Integer)
    price_prior_sweep_json: Mapped[list | None] = mapped_column(JSONB)
    default_price_prior_weight: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    default_price_prior_weight_mae: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    best_price_prior_weight: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    best_price_prior_weight_mae: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    price_prior_caveat: Mapped[str | None] = mapped_column(String(700))
    # The LIVE artifact app.optimization.projection._load_current_price_comparable_buckets reads
    # at prediction time -- see app.modeling.cold_start_validation.compute_price_comparable_buckets
    # for why this is NOT leakage-scoped the way the sweep above is (it pools every usable season,
    # since it predicts a future, not-yet-played one).
    price_comparable_buckets_json: Mapped[list | None] = mapped_column(JSONB)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_cold_start_validation_runs_job_id", "job_id"),)
