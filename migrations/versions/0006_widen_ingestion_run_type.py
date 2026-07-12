"""widen ingestion_runs.run_type column

``ingestion_runs.run_type`` is a ``native_enum=False`` ``VARCHAR`` sized to fit
the longest ``IngestionRunType`` value at the time it was created
(``"data_ingestion"`` = 14 chars, per ``information_schema.columns`` on the
live schema). Adding ``IngestionRunType.HISTORICAL_DATA_INGESTION``
("historical_data_ingestion" = 25 chars) requires widening the column.

Note ``jobs.job_type`` does *not* need widening here: it was already
``VARCHAR(25)`` (sized off the pre-existing ``"recommendation_generation"``
member), which exactly fits ``"historical_data_ingestion"`` (also 25 chars) —
confirmed via a live ``information_schema.columns`` query before writing this
migration, not by manual character-counting alone.

Sized to 30 for headroom, matching this codebase's convention elsewhere (see
0005 / ``projection_source String(50)``).

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-11

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "ingestion_runs",
        "run_type",
        type_=sa.String(length=30),
        existing_type=sa.String(length=14),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "ingestion_runs",
        "run_type",
        type_=sa.String(length=14),
        existing_type=sa.String(length=30),
        existing_nullable=False,
    )
