"""chip_schedule_optimization_runs.source_chip_optimization_run_id nullable

Phase 2 of the joint chip scheduler (see ``app/jobs/handlers/chip_schedule_optimization.py``'s
own module docstring for the full incident/design rationale) does its own FRESH, full-season
evaluation directly from a ``manager_squad_snapshot_id`` -- mirroring how ``CHIP_SIMULATION``
itself works -- rather than reading an existing ``CHIP_SIMULATION`` run's own persisted
candidates the way Phase 1 did. There is therefore no longer always a real source run to point
back to: this column stays populated for historical Phase-1-shaped rows and genuinely optional
going forward (kept, not dropped, since a caller can still supply one for cross-reference if they
happen to have a relevant ``CHIP_SIMULATION`` run in hand -- just never required).

Revision ID: 0048
Revises: 0047
Create Date: 2026-08-26

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("chip_schedule_optimization_runs", "source_chip_optimization_run_id", nullable=True)


def downgrade() -> None:
    op.alter_column("chip_schedule_optimization_runs", "source_chip_optimization_run_id", nullable=False)
