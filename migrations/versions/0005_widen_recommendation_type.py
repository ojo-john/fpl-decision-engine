"""widen recommendation_type column

The ``optimization_runs.recommendation_type`` column is a ``native_enum=False``
``VARCHAR`` sized to fit the longest enum value at the time it was created
(``"initial_squad"`` = 13 chars). Adding ``RecommendationType.JOINT_SQUAD_AND_XI``
("joint_squad_and_xi" = 19 chars) requires widening the column. Sized to 30
for headroom, matching this codebase's convention elsewhere (e.g.
``projection_source String(50)``).

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-11

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "optimization_runs",
        "recommendation_type",
        type_=sa.String(length=30),
        existing_type=sa.String(length=13),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "optimization_runs",
        "recommendation_type",
        type_=sa.String(length=13),
        existing_type=sa.String(length=30),
        existing_nullable=False,
    )
