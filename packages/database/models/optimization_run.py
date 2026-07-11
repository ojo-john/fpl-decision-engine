"""The ``optimization_runs`` table: one row per SQUAD_OPTIMIZATION/LINEUP_OPTIMIZATION job run.

This is the recommendation-artifact header row — the concrete per-player
picks live in ``optimization_run_players`` (see that model's docstring).
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean as SABoolean
from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, Text, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.domain.optimization import (
    OptimizationRunStatus,
    RecommendationType,
    SolverStatus,
    TargetGameweekStatus,
)
from packages.domain.rules import RulesStatus


class OptimizationRun(Base):
    __tablename__ = "optimization_runs"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("jobs.id"))
    season_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("seasons.id"), nullable=False
    )
    rules_config_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("rules_config.id"), nullable=False
    )
    recommendation_type: Mapped[RecommendationType] = mapped_column(
        SAEnum(
            RecommendationType,
            name="optimization_run_recommendation_type",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
    )
    source_optimization_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("optimization_runs.id")
    )
    run_status: Mapped[OptimizationRunStatus] = mapped_column(
        SAEnum(
            OptimizationRunStatus, name="optimization_run_status", native_enum=False, validate_strings=True
        ),
        nullable=False,
        default=OptimizationRunStatus.RUNNING,
    )
    solver_status: Mapped[SolverStatus | None] = mapped_column(
        SAEnum(SolverStatus, name="optimization_run_solver_status", native_enum=False, validate_strings=True)
    )
    target_gameweek_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("gameweeks.id")
    )
    target_gameweek_status: Mapped[TargetGameweekStatus] = mapped_column(
        SAEnum(
            TargetGameweekStatus,
            name="optimization_run_target_gameweek_status",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
    )
    squad_player_ids: Mapped[list | None] = mapped_column(JSONB)
    starting_xi_player_ids: Mapped[list | None] = mapped_column(JSONB)
    bench_player_ids_ordered: Mapped[list | None] = mapped_column(JSONB)
    captain_player_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("players.id")
    )
    vice_captain_player_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("players.id")
    )
    squad_total_cost: Mapped[int | None] = mapped_column(Integer)
    squad_projected_points: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    xi_projected_points: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    optimization_permitted_snapshot: Mapped[bool] = mapped_column(SABoolean, nullable=False)
    data_quality_score_snapshot: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    rules_status_snapshot: Mapped[RulesStatus] = mapped_column(
        SAEnum(
            RulesStatus,
            name="optimization_run_rules_status_snapshot",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
    )
    projection_formula_version: Mapped[int] = mapped_column(Integer, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    parameters_json: Mapped[dict | None] = mapped_column(JSONB)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_optimization_runs_season_recommendation_type", "season_id", "recommendation_type"),
    )
