"""recommendation audits table

Adds ``recommendation_audits`` -- Milestone 7 Layer 1 (Recursive Learning
Loop: Recommendation Correlation/Audit)'s single new table. Correlates the
most recent SUCCEEDED run of each of the 5 existing recommendation-producing
job handlers (squad/lineup optimization, transfer optimization, H2H
simulation, chip simulation) for one target gameweek, plus generation-time
context and a mechanically-derived confidence/assumptions/known-risks
summary. See ``packages/database/models/recommendation_audit.py`` for the
full column docstrings and design rationale.

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

from packages.domain.ingestion import EndpointHealth
from packages.domain.learning import RecommendationConfidence

# revision identifiers, used by Alembic.
revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "recommendation_audits",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("job_id", UUID(as_uuid=True), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column("season_id", UUID(as_uuid=True), sa.ForeignKey("seasons.id"), nullable=False),
        sa.Column("target_gameweek_id", UUID(as_uuid=True), sa.ForeignKey("gameweeks.id"), nullable=False),
        sa.Column("target_fpl_event_id", sa.Integer(), nullable=False),
        sa.Column("is_authoritative", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "optimization_run_id", UUID(as_uuid=True), sa.ForeignKey("optimization_runs.id"), nullable=True
        ),
        sa.Column(
            "transfer_optimization_run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("transfer_optimization_runs.id"),
            nullable=True,
        ),
        sa.Column(
            "h2h_simulation_run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("h2h_simulation_runs.id"),
            nullable=True,
        ),
        sa.Column(
            "chip_optimization_run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("chip_optimization_runs.id"),
            nullable=True,
        ),
        sa.Column(
            "champion_model_version_id",
            UUID(as_uuid=True),
            sa.ForeignKey("model_versions.id"),
            nullable=True,
        ),
        sa.Column(
            "fpl_api_health_at_generation",
            sa.Enum(
                EndpointHealth,
                name="recommendation_audit_fpl_api_health",
                native_enum=False,
                validate_strings=True,
            ),
            nullable=True,
        ),
        sa.Column("application_version", sa.String(length=50), nullable=True),
        sa.Column(
            "confidence",
            sa.Enum(
                RecommendationConfidence,
                name="recommendation_audit_confidence",
                native_enum=False,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column("confidence_drivers_json", JSONB(), nullable=True),
        sa.Column("assumptions_json", JSONB(), nullable=True),
        sa.Column("known_risks_json", JSONB(), nullable=True),
        sa.Column("user_actual_decision_json", JSONB(), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_recommendation_audits_gameweek_authoritative",
        "recommendation_audits",
        ["season_id", "target_gameweek_id"],
        unique=True,
        postgresql_where=sa.text("is_authoritative"),
    )
    op.create_index(
        "ix_recommendation_audits_season_gameweek",
        "recommendation_audits",
        ["season_id", "target_gameweek_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_recommendation_audits_season_gameweek", table_name="recommendation_audits")
    op.drop_index("ix_recommendation_audits_gameweek_authoritative", table_name="recommendation_audits")
    op.drop_table("recommendation_audits")
