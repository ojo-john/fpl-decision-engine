"""chip window candidate h2h win probability

Adds ``h2h_win_probability_with_chip``/``h2h_win_probability_without_chip``
(``Numeric(5,4)``, nullable) to ``chip_window_candidates`` -- Milestone 6
Layer 4 (league-context refinement): for the IMMEDIATE next-gameweek
candidate only, when a real scheduled H2H opponent can be resolved for that
exact gameweek, these record a paired Monte Carlo win-probability comparison
(with vs. without the chip) against that real rival's predicted state. NULL
on every other candidate row, and NULL on the immediate row too whenever no
real opponent/profile/snapshot can be resolved -- see
``packages/database/models/chip_window_candidate.py`` and
``app/jobs/handlers/chip_simulation.py`` for the full resolution chain and
design rationale.

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-15

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chip_window_candidates",
        sa.Column("h2h_win_probability_with_chip", sa.Numeric(5, 4), nullable=True),
    )
    op.add_column(
        "chip_window_candidates",
        sa.Column("h2h_win_probability_without_chip", sa.Numeric(5, 4), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chip_window_candidates", "h2h_win_probability_without_chip")
    op.drop_column("chip_window_candidates", "h2h_win_probability_with_chip")
