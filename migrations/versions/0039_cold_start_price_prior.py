"""cold start validation runs: price-informed true-rookie fallback columns

Extends cold_start_validation_runs with a fourth, structurally different piece of validation: the
price-informed true-rookie fallback's own blend-weight sweep results, and the LIVE
price-comparable-bucket lookup table app.optimization.projection._price_informed_true_rookie_projection
reads at prediction time. See packages/database/models/cold_start_validation_run.py's own updated
docstring and app/modeling/cold_start_validation.py for the full design.

Renumbered at merge time from this worktree's original "0036" (branched off main at 308adf6, before
0036-0038 -- gameweek_schedule_gap_days, start_probability_models, position_segmentation_validation_runs
-- landed). Rebased onto 0038, since this migration extends cold_start_validation_runs (created by
0034) with new columns and must chain after everything else that touched migrations/versions/ tonight.

Revision ID: 0039
Revises: 0038
Create Date: 2026-08-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "cold_start_validation_runs", sa.Column("n_true_rookie_observations", sa.Integer(), nullable=True)
    )
    op.add_column("cold_start_validation_runs", sa.Column("price_prior_sweep_json", JSONB(), nullable=True))
    op.add_column(
        "cold_start_validation_runs",
        sa.Column("default_price_prior_weight", sa.Numeric(precision=6, scale=4), nullable=True),
    )
    op.add_column(
        "cold_start_validation_runs",
        sa.Column("default_price_prior_weight_mae", sa.Numeric(precision=8, scale=4), nullable=True),
    )
    op.add_column(
        "cold_start_validation_runs",
        sa.Column("best_price_prior_weight", sa.Numeric(precision=6, scale=4), nullable=True),
    )
    op.add_column(
        "cold_start_validation_runs",
        sa.Column("best_price_prior_weight_mae", sa.Numeric(precision=8, scale=4), nullable=True),
    )
    op.add_column(
        "cold_start_validation_runs", sa.Column("price_prior_caveat", sa.String(length=700), nullable=True)
    )
    op.add_column(
        "cold_start_validation_runs", sa.Column("price_comparable_buckets_json", JSONB(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("cold_start_validation_runs", "price_comparable_buckets_json")
    op.drop_column("cold_start_validation_runs", "price_prior_caveat")
    op.drop_column("cold_start_validation_runs", "best_price_prior_weight_mae")
    op.drop_column("cold_start_validation_runs", "best_price_prior_weight")
    op.drop_column("cold_start_validation_runs", "default_price_prior_weight_mae")
    op.drop_column("cold_start_validation_runs", "default_price_prior_weight")
    op.drop_column("cold_start_validation_runs", "price_prior_sweep_json")
    op.drop_column("cold_start_validation_runs", "n_true_rookie_observations")
