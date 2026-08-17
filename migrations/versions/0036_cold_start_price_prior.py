"""cold start validation runs: price-informed true-rookie fallback columns

Extends cold_start_validation_runs with a fourth, structurally different piece of validation: the
price-informed true-rookie fallback's own blend-weight sweep results, and the LIVE
price-comparable-bucket lookup table app.optimization.projection._price_informed_true_rookie_projection
reads at prediction time. See packages/database/models/cold_start_validation_run.py's own updated
docstring and app/modeling/cold_start_validation.py for the full design.

NOTE for whoever reconciles this with main: this worktree branched off main at commit 308adf6 (the
latest merged migration there is 0035_goal_involvement_and_defensive_contribution.py). Other agents
working in parallel this session may have independently claimed "0036" too -- if so, this migration
needs the same renumber-and-rebase treatment 0033/0034/0035 already went through (see those
migrations' own docstrings for the established pattern: keep whichever landed on main first,
renumber the other to chain after it, re-run alembic upgrade head against both .env.test and the
real dev database once reconciled).

Revision ID: 0036
Revises: 0035
Create Date: 2026-08-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0036"
down_revision: str | None = "0035"
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
