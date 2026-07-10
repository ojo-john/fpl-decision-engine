"""create jobs table

Revision ID: 0001
Revises:
Create Date: 2026-07-09

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

from packages.domain.jobs import JobStatus, JobType, TriggerType

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "job_type",
            sa.Enum(JobType, name="job_type", native_enum=False, validate_strings=True),
            nullable=False,
        ),
        sa.Column("requested_by", sa.String(length=255), nullable=True),
        sa.Column(
            "trigger_type",
            sa.Enum(TriggerType, name="trigger_type", native_enum=False, validate_strings=True),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(JobStatus, name="job_status", native_enum=False, validate_strings=True),
            nullable=False,
        ),
        sa.Column("parameters_json", JSONB(), nullable=True),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("git_commit_sha", sa.String(length=40), nullable=True),
        sa.Column("application_version", sa.String(length=50), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("output_json", JSONB(), nullable=True),
        sa.Column("logs_uri", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_jobs_idempotency_key",
        "jobs",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_idempotency_key", table_name="jobs")
    op.drop_table("jobs")
