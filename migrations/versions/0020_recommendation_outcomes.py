"""recommendation outcomes and counterfactual results tables

Adds ``recommendation_outcomes`` and ``counterfactual_results`` -- Milestone 7
Layer 2 (Recursive Learning Loop: Outcome Ingestion/Counterfactual Engine)'s
two new tables. ``recommendation_outcomes`` records what actually happened for
one ``recommendation_audits`` row once its target gameweek finishes;
``counterfactual_results`` records each individually-computed
baseline-vs-alternative comparison for that outcome. See
``packages/database/models/recommendation_outcome.py`` and
``packages/database/models/counterfactual_result.py`` for the full column
docstrings and design rationale.

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

from packages.domain.learning import CounterfactualComparisonType

# revision identifiers, used by Alembic.
revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "recommendation_outcomes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "recommendation_audit_id",
            UUID(as_uuid=True),
            sa.ForeignKey("recommendation_audits.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("actual_xi_points", sa.Integer(), nullable=True),
        sa.Column("actual_captain_points", sa.Integer(), nullable=True),
        sa.Column("actual_bench_points", sa.Integer(), nullable=True),
        sa.Column("h2h_match_id", UUID(as_uuid=True), sa.ForeignKey("h2h_matches.id"), nullable=True),
        sa.Column("h2h_result", sa.String(length=10), nullable=True),
        sa.Column("opponent_actual_squad_player_ids_json", JSONB(), nullable=True),
        sa.Column("opponent_actual_captain_player_id", sa.Integer(), nullable=True),
        sa.Column("actual_transfers_made_json", JSONB(), nullable=True),
        sa.Column("actual_hit_cost_taken", sa.Integer(), nullable=True),
        sa.Column("actual_chip_played", sa.String(length=20), nullable=True),
        sa.Column("recommendation_net_gain", sa.Numeric(precision=6, scale=2), nullable=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "counterfactual_results",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "recommendation_outcome_id",
            UUID(as_uuid=True),
            sa.ForeignKey("recommendation_outcomes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "comparison_type",
            sa.Enum(
                CounterfactualComparisonType,
                name="counterfactual_result_comparison_type",
                native_enum=False,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column("baseline_points", sa.Numeric(precision=6, scale=2), nullable=False),
        sa.Column("alternative_points", sa.Numeric(precision=6, scale=2), nullable=False),
        sa.Column("delta", sa.Numeric(precision=6, scale=2), nullable=False),
        sa.Column("is_value_adding", sa.Boolean(), nullable=False),
        sa.Column("baseline_description_json", JSONB(), nullable=True),
        sa.Column("alternative_description_json", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "recommendation_outcome_id", "comparison_type", name="uq_counterfactual_results_outcome_type"
        ),
    )
    op.create_index(
        "ix_counterfactual_results_outcome_id", "counterfactual_results", ["recommendation_outcome_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_counterfactual_results_outcome_id", table_name="counterfactual_results")
    op.drop_table("counterfactual_results")
    op.drop_table("recommendation_outcomes")
