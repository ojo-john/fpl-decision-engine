"""chip schedule optimization tables

Adds ``chip_schedule_optimization_runs`` (insert-only run-header, one row per
CHIP_SCHEDULE_OPTIMIZATION job run) and ``chip_schedule_assignments`` (insert-only child, one row
per requested chip type per run, recording the assignment solver's final decision) for Phase 1 of
the joint chip scheduler. Real gap this closes: ``CHIP_SIMULATION`` evaluates each of the 4 chips
completely independently, so it can (and did, live) recommend "use now" for multiple chips in the
SAME gameweek -- a real FPL manager can only play one chip per gameweek. This job consumes an
existing, already-SUCCEEDED ``chip_optimization_runs`` row's own ``chip_window_candidates`` (fixed,
already-computed gain estimates) and picks the best non-colliding gameweek-to-chip assignment. See
``packages/database/models/chip_schedule_optimization_run.py``,
``packages/database/models/chip_schedule_assignment.py``, and
``app/jobs/handlers/chip_schedule_optimization.py`` for the full column docstrings and design
rationale.

Revision ID: 0047
Revises: 0046
Create Date: 2026-08-26

"""

import sys
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# Some alembic commands (e.g. `alembic history`) load version scripts
# without first running env.py (which normally inserts the repo root onto
# sys.path) — so this file adds the same path fixup defensively, letting it
# import `packages.*` regardless of which alembic command loads it.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from packages.domain.chips import ChipScheduleRunStatus, ChipType  # noqa: E402
from packages.domain.optimization import SolverStatus  # noqa: E402

# revision identifiers, used by Alembic.
revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chip_schedule_optimization_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("job_id", UUID(as_uuid=True), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column(
            "source_chip_optimization_run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("chip_optimization_runs.id"),
            nullable=False,
        ),
        sa.Column(
            "manager_squad_snapshot_id",
            UUID(as_uuid=True),
            sa.ForeignKey("manager_squad_snapshots.id"),
            nullable=False,
        ),
        sa.Column("season_id", UUID(as_uuid=True), sa.ForeignKey("seasons.id"), nullable=False),
        sa.Column(
            "run_status",
            sa.Enum(
                ChipScheduleRunStatus,
                name="chip_schedule_optimization_run_status",
                native_enum=False,
                validate_strings=True,
                # Explicit real width chosen at creation time (64, matching this schema's own
                # established convention -- see tests/unit/test_enum_column_widths.py's own
                # docstring for the real, recurring bug class this avoids: a native_enum=False
                # column with no explicit length gets silently sized to whatever the enum's
                # longest member happens to be RIGHT NOW, never auto-widened later).
                length=64,
            ),
            nullable=False,
        ),
        sa.Column(
            "solver_status",
            sa.Enum(
                SolverStatus,
                name="chip_schedule_optimization_run_solver_status",
                native_enum=False,
                validate_strings=True,
                length=64,
            ),
            nullable=True,
        ),
        sa.Column("total_expected_gain", sa.Numeric(6, 2), nullable=True),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "chip_schedule_assignments",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "chip_schedule_optimization_run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("chip_schedule_optimization_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "chip_type",
            sa.Enum(
                ChipType,
                name="chip_schedule_assignment_chip_type",
                native_enum=False,
                validate_strings=True,
                length=64,
            ),
            nullable=False,
        ),
        sa.Column("window_number", sa.Integer(), nullable=False),
        sa.Column("assigned_fpl_event_id", sa.Integer(), nullable=True),
        sa.Column("gain_mean", sa.Numeric(6, 2), nullable=True),
        sa.Column(
            "source_chip_window_candidate_id",
            UUID(as_uuid=True),
            sa.ForeignKey("chip_window_candidates.id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "chip_schedule_optimization_run_id",
            "chip_type",
            "window_number",
            name="uq_chip_schedule_assignments_run_chip_window",
        ),
    )
    op.create_index(
        "ix_chip_schedule_assignments_run_id",
        "chip_schedule_assignments",
        ["chip_schedule_optimization_run_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_chip_schedule_assignments_run_id", table_name="chip_schedule_assignments")
    op.drop_table("chip_schedule_assignments")
    op.drop_table("chip_schedule_optimization_runs")
