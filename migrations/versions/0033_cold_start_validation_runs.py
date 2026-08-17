"""cold start validation runs table

Historical validation of app.optimization.projection._cold_start_projection's hand-written blend
formula (never covered by BACKTEST/MODEL_TRAINING's own walk-forward validation, which only ever
evaluate a trained model_versions row). One insert-only row per COLD_START_PROJECTION_VALIDATION job
run -- see packages/database/models/cold_start_validation_run.py for the full column docstrings and
design rationale, and app/modeling/cold_start_validation.py for the evaluation harness itself.

NOTE for whoever reconciles this with main: this worktree's own migration chain here is
self-consistent (0033 following 0032). However, the shared local dev Postgres this worktree's
DATABASE_URL originally pointed at (``fpl_decision_engine_test``) was found, mid-session, to
already have a DIFFERENT "0033" migration applied by a concurrent, unrelated agent session (a
"note_effectiveness_evaluations" table also based on 0032) -- a real revision-id collision the
two branches' migration histories will have to resolve (``alembic merge heads`` or a manual
renumber) before both can land on main. To avoid interfering with that concurrent work, this
session's own test/verification runs were performed against an isolated database dedicated to
this worktree instead of the shared ``fpl_decision_engine_test`` (see this repo's own
ARCHITECTURE.md cold-start-validation section / this session's final report for the exact name
used) -- never against the shared one, and never against the real dev database either.

Revision ID: 0033
Revises: 0032
Create Date: 2026-08-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

from packages.domain.learning import ColdStartValidationRunStatus

# revision identifiers, used by Alembic.
revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cold_start_validation_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("job_id", UUID(as_uuid=True), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column(
            "run_status",
            sa.Enum(
                ColdStartValidationRunStatus,
                name="cold_start_validation_run_status",
                native_enum=False,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column("early_season_gameweek_cutoff", sa.Integer(), nullable=False),
        sa.Column("component_evaluated", sa.String(length=100), nullable=False),
        sa.Column("usable_seasons_json", JSONB(), nullable=False),
        sa.Column("seasons_evaluated_json", JSONB(), nullable=False),
        sa.Column("n_observations", sa.Integer(), nullable=False),
        sa.Column("half_life_sweep_json", JSONB(), nullable=False),
        sa.Column("default_half_life_seasons", sa.Numeric(precision=12, scale=4), nullable=False),
        sa.Column("default_half_life_mae", sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column("best_half_life_seasons", sa.Numeric(precision=12, scale=4), nullable=False),
        sa.Column("best_half_life_label", sa.String(length=30), nullable=False),
        sa.Column("best_half_life_mae", sa.Numeric(precision=8, scale=4), nullable=False),
        sa.Column("difficulty_coefficient_sweep_json", JSONB(), nullable=False),
        sa.Column("default_difficulty_coefficient", sa.Numeric(precision=6, scale=4), nullable=False),
        sa.Column("default_difficulty_coefficient_mae", sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column("best_difficulty_coefficient", sa.Numeric(precision=6, scale=4), nullable=True),
        sa.Column("best_difficulty_coefficient_mae", sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column("blend_ratio_sweep_json", JSONB(), nullable=False),
        sa.Column("default_blend_career_avg_weight", sa.Numeric(precision=6, scale=4), nullable=False),
        sa.Column("default_blend_mae", sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column("best_blend_career_avg_weight", sa.Numeric(precision=6, scale=4), nullable=True),
        sa.Column("best_blend_mae", sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column("blend_ratio_caveat", sa.String(length=500), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_cold_start_validation_runs_job_id", "cold_start_validation_runs", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_cold_start_validation_runs_job_id", table_name="cold_start_validation_runs")
    op.drop_table("cold_start_validation_runs")
