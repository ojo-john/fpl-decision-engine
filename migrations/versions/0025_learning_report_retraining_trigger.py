"""learning report retraining trigger columns

Milestone 7 Layer 6 (Recursive Learning Loop: BACKTEST job + the
GAMEWEEK_REVIEW -> MODEL_TRAINING retraining-trigger wiring)'s second schema
change: adds ``learning_reports.retraining_triggered``/``retraining_job_id``.
Once a GAMEWEEK_REVIEW run's own ``ROLLING_PROJECTION_MAE`` learning signal
comes back with ``threshold_breached=True``, ``app.jobs.handlers
.gameweek_review`` triggers a ``MODEL_TRAINING`` job and records the outcome
via these two columns. Both nullable, same "``NULL`` until this step actually
runs" convention as every other late-populated ``learning_reports`` column
(``recommendation_outcome_id``, etc.) -- see
``packages/database/models/learning_report.py`` for the full rationale.

Revision ID: 0025
Revises: 0024
Create Date: 2026-07-19

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("learning_reports", sa.Column("retraining_triggered", sa.Boolean(), nullable=True))
    op.add_column(
        "learning_reports",
        sa.Column("retraining_job_id", UUID(as_uuid=True), sa.ForeignKey("jobs.id"), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("learning_reports", "retraining_job_id")
    op.drop_column("learning_reports", "retraining_triggered")
