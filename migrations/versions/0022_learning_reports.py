"""learning reports table

Adds ``learning_reports`` -- Milestone 7 Layer 4 (Recursive Learning Loop:
GAMEWEEK_REVIEW orchestration)'s one new table. One insert-only row per
GAMEWEEK_REVIEW job run, summarizing the end-to-end review this job performed
(Layer 2's outcome ingestion, Layer 3's failure attribution and rolling
learning signals) for one already-finished gameweek's authoritative
recommendation. See ``packages/database/models/learning_report.py`` for the
full column docstrings and design rationale.

Revision ID: 0022
Revises: 0021
Create Date: 2026-07-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

from packages.domain.learning import GameweekReviewRunStatus

# revision identifiers, used by Alembic.
revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "learning_reports",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("job_id", UUID(as_uuid=True), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column("season_id", UUID(as_uuid=True), sa.ForeignKey("seasons.id"), nullable=False),
        sa.Column("gameweek_id", UUID(as_uuid=True), sa.ForeignKey("gameweeks.id"), nullable=False),
        sa.Column(
            "run_status",
            sa.Enum(
                GameweekReviewRunStatus,
                name="learning_report_run_status",
                native_enum=False,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "recommendation_outcome_id",
            UUID(as_uuid=True),
            sa.ForeignKey("recommendation_outcomes.id"),
            nullable=True,
        ),
        sa.Column("failure_attribution_count", sa.Integer(), nullable=True),
        sa.Column("learning_signals_computed_json", JSONB(), nullable=True),
        sa.Column("report_markdown", sa.Text(), nullable=True),
        sa.Column("summary_json", JSONB(), nullable=True),
        sa.Column("notable_findings_json", JSONB(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_learning_reports_season_gameweek", "learning_reports", ["season_id", "gameweek_id"])
    op.create_index("ix_learning_reports_job_id", "learning_reports", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_learning_reports_job_id", table_name="learning_reports")
    op.drop_index("ix_learning_reports_season_gameweek", table_name="learning_reports")
    op.drop_table("learning_reports")
