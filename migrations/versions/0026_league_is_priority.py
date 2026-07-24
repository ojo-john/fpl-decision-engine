"""leagues.is_priority

A manager can sync more than one real FPL H2H league (fully supported since
Milestone 8 -- ``POST /leagues/sync`` has no cap), but the Rivals page needs
exactly one to show by default. Adds ``leagues.is_priority`` with the same
"exactly one true row at a time, application-enforced, no DB constraint"
invariant this codebase already uses for ``seasons.is_current``/
``model_versions.is_champion`` -- see ``app.jobs.handlers.league_sync``'s
own bootstrap logic (the first league ever synced becomes priority
automatically; every league synced after that stays non-priority until a
manager explicitly calls ``POST /leagues/{league_id}/priority``).

Revision ID: 0026
Revises: 0025
Create Date: 2026-07-24

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "leagues",
        sa.Column("is_priority", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("leagues", "is_priority")
