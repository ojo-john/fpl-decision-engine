"""cold start validation runs: defensive-contribution reliability sweep columns

Extends cold_start_validation_runs with a fifth, structurally different piece of validation: a
DEF/MID-only "trust this player's aggregate history more when it's demonstrably driven by a
repeatable defensive-work-rate skill" adjustment sweep. Unlike the other four sweeps already
persisted here, this one is NOT wired into production -- it currently, honestly, always evaluates
zero observations, since defensive_contribution (FPL's real 2025-26+ CBIT/CBIRT bonus stat) only
has one real season of data, and leakage-safe validation needs a strictly-prior season's own DC
data to predict a later, already-known season's outcome. See
packages/database/models/cold_start_validation_run.py's own updated docstring and
app/modeling/cold_start_validation.py's DC_RELIABILITY_COEFFICIENT_SWEEP_CANDIDATES for the full
design and the real, honest reason this currently has nothing to conclude.

Revision ID: 0042
Revises: 0041
Create Date: 2026-08-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "cold_start_validation_runs",
        sa.Column("n_dc_reliability_observations", sa.Integer(), nullable=True),
    )
    op.add_column(
        "cold_start_validation_runs", sa.Column("dc_reliability_sweep_json", JSONB(), nullable=True)
    )
    op.add_column(
        "cold_start_validation_runs",
        sa.Column("default_dc_reliability_coefficient", sa.Numeric(precision=6, scale=4), nullable=True),
    )
    op.add_column(
        "cold_start_validation_runs",
        sa.Column("default_dc_reliability_coefficient_mae", sa.Numeric(precision=8, scale=4), nullable=True),
    )
    op.add_column(
        "cold_start_validation_runs",
        sa.Column("best_dc_reliability_coefficient", sa.Numeric(precision=6, scale=4), nullable=True),
    )
    op.add_column(
        "cold_start_validation_runs",
        sa.Column("best_dc_reliability_coefficient_mae", sa.Numeric(precision=8, scale=4), nullable=True),
    )
    op.add_column(
        "cold_start_validation_runs", sa.Column("dc_reliability_caveat", sa.String(length=700), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("cold_start_validation_runs", "dc_reliability_caveat")
    op.drop_column("cold_start_validation_runs", "best_dc_reliability_coefficient_mae")
    op.drop_column("cold_start_validation_runs", "best_dc_reliability_coefficient")
    op.drop_column("cold_start_validation_runs", "default_dc_reliability_coefficient_mae")
    op.drop_column("cold_start_validation_runs", "default_dc_reliability_coefficient")
    op.drop_column("cold_start_validation_runs", "dc_reliability_sweep_json")
    op.drop_column("cold_start_validation_runs", "n_dc_reliability_observations")
