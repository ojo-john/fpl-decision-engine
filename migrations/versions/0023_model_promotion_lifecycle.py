"""model promotion lifecycle

Milestone 7 Layer 5 (Recursive Learning Loop: Challenger Training/Shadow-Mode
Evaluation/Promotion Gates/Rollback):

1. Adds ``model_versions.lifecycle_state`` (backfilled from the existing
   ``is_champion`` flag: ``True`` -> ``champion``, everything else ->
   ``candidate`` -- the honest default for every pre-existing row, since none
   of them were ever a shadow challenger, rejected challenger, or retired
   champion under a system that didn't exist yet).
2. Adds ``model_challenger_evaluations`` -- one row per gameweek a shadow
   challenger is scored against the current champion.
3. Adds ``model_promotions`` -- one insert-only row per promotion decision
   (promoted/rejected/rolled back).

See ``packages/database/models/model_version.py``,
``model_challenger_evaluation.py``, and ``model_promotion.py`` for the full
column docstrings and design rationale, and ``app.modeling.promotion``/
``app.modeling.shadow_evaluation`` for the code that populates these.

Revision ID: 0023
Revises: 0022
Create Date: 2026-07-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

from packages.domain.learning import ModelLifecycleState, PromotionVerdict

# revision identifiers, used by Alembic.
revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LIFECYCLE_STATE_ENUM = sa.Enum(
    ModelLifecycleState, name="model_version_lifecycle_state", native_enum=False, validate_strings=True
)


def upgrade() -> None:
    # --- model_versions.lifecycle_state: add nullable, backfill, then enforce NOT NULL -------------
    op.add_column(
        "model_versions",
        sa.Column("lifecycle_state", _LIFECYCLE_STATE_ENUM, nullable=True),
    )
    op.execute(
        "UPDATE model_versions "
        "SET lifecycle_state = CASE WHEN is_champion THEN 'champion' ELSE 'candidate' END"
    )
    op.alter_column(
        "model_versions",
        "lifecycle_state",
        nullable=False,
        server_default=ModelLifecycleState.CANDIDATE.value,
    )
    op.create_index("ix_model_versions_lifecycle_state", "model_versions", ["lifecycle_state"])

    # --- model_challenger_evaluations ----------------------------------------------------------------
    op.create_table(
        "model_challenger_evaluations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "challenger_model_version_id",
            UUID(as_uuid=True),
            sa.ForeignKey("model_versions.id"),
            nullable=False,
        ),
        sa.Column(
            "champion_model_version_id",
            UUID(as_uuid=True),
            sa.ForeignKey("model_versions.id"),
            nullable=False,
        ),
        sa.Column("season_id", UUID(as_uuid=True), sa.ForeignKey("seasons.id"), nullable=False),
        sa.Column("gameweek_id", UUID(as_uuid=True), sa.ForeignKey("gameweeks.id"), nullable=False),
        sa.Column("challenger_mae", sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column("champion_mae", sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column("n_observations", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "challenger_model_version_id",
            "gameweek_id",
            name="uq_model_challenger_evaluations_challenger_gameweek",
        ),
    )
    op.create_index(
        "ix_model_challenger_evaluations_challenger_id",
        "model_challenger_evaluations",
        ["challenger_model_version_id"],
    )
    op.create_index(
        "ix_model_challenger_evaluations_gameweek_id", "model_challenger_evaluations", ["gameweek_id"]
    )

    # --- model_promotions -----------------------------------------------------------------------------
    op.create_table(
        "model_promotions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "challenger_model_version_id",
            UUID(as_uuid=True),
            sa.ForeignKey("model_versions.id"),
            nullable=False,
        ),
        sa.Column(
            "previous_champion_model_version_id",
            UUID(as_uuid=True),
            sa.ForeignKey("model_versions.id"),
            nullable=True,
        ),
        sa.Column(
            "verdict",
            sa.Enum(
                PromotionVerdict, name="model_promotion_verdict", native_enum=False, validate_strings=True
            ),
            nullable=False,
        ),
        sa.Column("gate_results_json", JSONB(), nullable=True),
        sa.Column("evaluation_window_gameweeks", sa.Integer(), nullable=True),
        sa.Column("n_shadow_evaluations", sa.Integer(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by_job_id", UUID(as_uuid=True), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column("report_markdown", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_model_promotions_challenger_id", "model_promotions", ["challenger_model_version_id"])
    op.create_index("ix_model_promotions_decided_at", "model_promotions", ["decided_at"])


def downgrade() -> None:
    op.drop_index("ix_model_promotions_decided_at", table_name="model_promotions")
    op.drop_index("ix_model_promotions_challenger_id", table_name="model_promotions")
    op.drop_table("model_promotions")

    op.drop_index("ix_model_challenger_evaluations_gameweek_id", table_name="model_challenger_evaluations")
    op.drop_index("ix_model_challenger_evaluations_challenger_id", table_name="model_challenger_evaluations")
    op.drop_table("model_challenger_evaluations")

    op.drop_index("ix_model_versions_lifecycle_state", table_name="model_versions")
    op.drop_column("model_versions", "lifecycle_state")
