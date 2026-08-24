"""push_subscriptions table

Milestone 9 Layer 1 (proactive alerting). Entirely new and additive (no existing table's columns
change). One row per browser/device subscribed to Web Push -- see
packages/database/models/push_subscription.py for the full column docstring on why this is a flat
list (no per-user identity exists anywhere in this single-shared-secret system) rather than a
per-user table.

Revision ID: 0044
Revises: 0043
Create Date: 2026-08-23

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "push_subscriptions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("endpoint", sa.String(), nullable=False, unique=True),
        sa.Column("p256dh_key", sa.String(), nullable=False),
        sa.Column("auth_key", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("push_subscriptions")
