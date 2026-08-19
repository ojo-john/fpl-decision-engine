"""widen every remaining narrow native_enum=False VARCHAR column (schema-wide audit)

Real, systemic risk class directly following from migration 0041's own fix, which covered
``jobs.job_type``/``status``/``trigger_type`` specifically and was explicitly documented as NOT
a full schema-wide audit. This migration is that audit.

Every ``native_enum=False`` ``SAEnum(...)`` column across this schema that lacks an explicit
``length=`` argument gets its real width fixed by Alembic/SQLAlchemy at whatever the backing
Python enum's longest member was the FIRST TIME its table's creating migration ran against a
given database -- not automatically kept in sync with later additions to that enum. A fresh
database created via `alembic upgrade head` today auto-sizes every such column against the
CURRENT enum and never shows this bug at all; only an already-migrated database (like the real
dev database) can have a column narrower than what the current enum needs -- which is exactly
why this entire class of bug was invisible to every integration test run this session (a fresh
`fpl_decision_engine_test` on every CI run) and only ever surfaced against the real, persistent
dev database, three separate times now (`ingestion_runs.run_type` in migration 0006;
`jobs.job_type`/`status`/`trigger_type` in migration 0041; and now this).

Audited via a real, live `information_schema.columns` query against the real dev database
(not recomputed from the current Python enum state alone -- that only tells you what a FRESH
database would create, not what the real one actually has) cross-referenced against every
`native_enum=False` `SAEnum` column's real current enum-member max length. Of 46 such columns
found across the whole schema, 39 were confirmed to have ZERO real headroom right now -- their
real column width exactly equals their enum's current longest member, the identical "just got
lucky so far" state `jobs.job_type` was silently sitting in before `historical_data_ingestion`
(25 chars) finally exceeded its old 25-char width. Every single one of these 39 columns will
break the instant its own enum gains one more, longer member -- not a hypothetical, a near-
certainty given how many new `*RunStatus`/`*Verdict`/similar enums this project has added over
its lifetime. The remaining 7 enum columns (`chip_type`, `ingestion_runs.run_type`,
`optimization_runs.recommendation_type`) already carry real headroom (5-12 spare chars, from
earlier migrations' own deliberate over-sizing) and are left untouched -- this migration widens
only the 39 columns confirmed to need it, not every enum column that exists.

Widened uniformly to 64 chars -- generous headroom above even the longest real value found
(`learning_signals.signal_type`'s `"rolling_h2h_win_probability_calibration"`, 39 chars) without
being arbitrarily huge. See `tests/unit/test_enum_column_widths.py`'s new, fully generalized
regression test (not just this migration's own 39 columns -- it dynamically enumerates every
`native_enum=False` column across the entire ORM metadata and asserts each one's real chosen
width against its enum's current max length), which is the actual permanent fix: any future
migration that ever narrows one of these back down, or any new enum member on ANY of these
enums that would exceed 64 chars, fails this test immediately in CI -- against every database
state, not just a database old enough to have the bug -- rather than silently working in tests
while failing in production the way this exact bug already has three separate times.

Purely additive widening (VARCHAR(n) -> VARCHAR(64) for n < 64 in every case here) -- no data
migration, no risk to existing rows, safe to apply to both `.env.test` and the real dev database.

Revision ID: 0043
Revises: 0042
Create Date: 2026-08-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "backtest_runs",
        "run_status",
        type_=sa.String(length=64),
        existing_type=sa.String(length=9),
        existing_nullable=False,
    )
    op.alter_column(
        "chip_optimization_runs",
        "rules_status_snapshot",
        type_=sa.String(length=64),
        existing_type=sa.String(length=8),
        existing_nullable=False,
    )
    op.alter_column(
        "chip_optimization_runs",
        "run_status",
        type_=sa.String(length=64),
        existing_type=sa.String(length=9),
        existing_nullable=False,
    )
    op.alter_column(
        "chip_recommendations",
        "verdict",
        type_=sa.String(length=64),
        existing_type=sa.String(length=16),
        existing_nullable=False,
    )
    op.alter_column(
        "cold_start_validation_runs",
        "run_status",
        type_=sa.String(length=64),
        existing_type=sa.String(length=9),
        existing_nullable=False,
    )
    op.alter_column(
        "counterfactual_results",
        "comparison_type",
        type_=sa.String(length=64),
        existing_type=sa.String(length=29),
        existing_nullable=False,
    )
    op.alter_column(
        "data_quality_issues",
        "severity",
        type_=sa.String(length=64),
        existing_type=sa.String(length=8),
        existing_nullable=False,
    )
    op.alter_column(
        "failure_attributions",
        "category",
        type_=sa.String(length=64),
        existing_type=sa.String(length=20),
        existing_nullable=False,
    )
    op.alter_column(
        "failure_attributions",
        "reason_code",
        type_=sa.String(length=64),
        existing_type=sa.String(length=29),
        existing_nullable=False,
    )
    op.alter_column(
        "h2h_simulation_runs",
        "confidence",
        type_=sa.String(length=64),
        existing_type=sa.String(length=6),
        existing_nullable=True,
    )
    op.alter_column(
        "h2h_simulation_runs",
        "rules_status_snapshot",
        type_=sa.String(length=64),
        existing_type=sa.String(length=8),
        existing_nullable=False,
    )
    op.alter_column(
        "h2h_simulation_runs",
        "run_status",
        type_=sa.String(length=64),
        existing_type=sa.String(length=9),
        existing_nullable=False,
    )
    op.alter_column(
        "h2h_strategy_candidates",
        "strategy_label",
        type_=sa.String(length=64),
        existing_type=sa.String(length=20),
        existing_nullable=False,
    )
    op.alter_column(
        "ingestion_runs",
        "overall_health",
        type_=sa.String(length=64),
        existing_type=sa.String(length=8),
        existing_nullable=True,
    )
    op.alter_column(
        "ingestion_runs",
        "status",
        type_=sa.String(length=64),
        existing_type=sa.String(length=23),
        existing_nullable=False,
    )
    op.alter_column(
        "league_sync_runs",
        "fpl_api_health_snapshot",
        type_=sa.String(length=64),
        existing_type=sa.String(length=8),
        existing_nullable=True,
    )
    op.alter_column(
        "league_sync_runs",
        "run_status",
        type_=sa.String(length=64),
        existing_type=sa.String(length=23),
        existing_nullable=False,
    )
    op.alter_column(
        "leagues",
        "league_type",
        type_=sa.String(length=64),
        existing_type=sa.String(length=7),
        existing_nullable=False,
    )
    op.alter_column(
        "learning_reports",
        "run_status",
        type_=sa.String(length=64),
        existing_type=sa.String(length=9),
        existing_nullable=False,
    )
    op.alter_column(
        "learning_signals",
        "signal_type",
        type_=sa.String(length=64),
        existing_type=sa.String(length=39),
        existing_nullable=False,
    )
    op.alter_column(
        "manager_squad_snapshots",
        "source",
        type_=sa.String(length=64),
        existing_type=sa.String(length=12),
        existing_nullable=False,
    )
    op.alter_column(
        "model_promotions",
        "verdict",
        type_=sa.String(length=64),
        existing_type=sa.String(length=21),
        existing_nullable=False,
    )
    op.alter_column(
        "model_versions",
        "lifecycle_state",
        type_=sa.String(length=64),
        existing_type=sa.String(length=19),
        existing_nullable=False,
    )
    op.alter_column(
        "model_versions",
        "model_family",
        type_=sa.String(length=64),
        existing_type=sa.String(length=32),
        existing_nullable=False,
    )
    op.alter_column(
        "optimization_runs",
        "rules_status_snapshot",
        type_=sa.String(length=64),
        existing_type=sa.String(length=8),
        existing_nullable=False,
    )
    op.alter_column(
        "optimization_runs",
        "run_status",
        type_=sa.String(length=64),
        existing_type=sa.String(length=9),
        existing_nullable=False,
    )
    op.alter_column(
        "optimization_runs",
        "solver_status",
        type_=sa.String(length=64),
        existing_type=sa.String(length=10),
        existing_nullable=True,
    )
    op.alter_column(
        "optimization_runs",
        "target_gameweek_status",
        type_=sa.String(length=64),
        existing_type=sa.String(length=18),
        existing_nullable=False,
    )
    op.alter_column(
        "position_segmentation_validation_runs",
        "run_status",
        type_=sa.String(length=64),
        existing_type=sa.String(length=9),
        existing_nullable=False,
    )
    op.alter_column(
        "raw_api_snapshots",
        "health_status",
        type_=sa.String(length=64),
        existing_type=sa.String(length=8),
        existing_nullable=False,
    )
    op.alter_column(
        "recommendation_audits",
        "confidence",
        type_=sa.String(length=64),
        existing_type=sa.String(length=6),
        existing_nullable=False,
    )
    op.alter_column(
        "recommendation_audits",
        "fpl_api_health_at_generation",
        type_=sa.String(length=64),
        existing_type=sa.String(length=8),
        existing_nullable=True,
    )
    op.alter_column(
        "rival_gameweek_snapshots",
        "picks_availability",
        type_=sa.String(length=64),
        existing_type=sa.String(length=35),
        existing_nullable=False,
    )
    op.alter_column(
        "rival_profiles",
        "derived_free_transfers_confidence",
        type_=sa.String(length=64),
        existing_type=sa.String(length=6),
        existing_nullable=True,
    )
    op.alter_column(
        "rival_profiles",
        "overall_confidence",
        type_=sa.String(length=64),
        existing_type=sa.String(length=6),
        existing_nullable=False,
    )
    op.alter_column(
        "rules_config",
        "rules_status",
        type_=sa.String(length=64),
        existing_type=sa.String(length=8),
        existing_nullable=False,
    )
    op.alter_column(
        "rules_config_fields",
        "confidence",
        type_=sa.String(length=64),
        existing_type=sa.String(length=11),
        existing_nullable=False,
    )
    op.alter_column(
        "transfer_optimization_runs",
        "rules_status_snapshot",
        type_=sa.String(length=64),
        existing_type=sa.String(length=8),
        existing_nullable=False,
    )
    op.alter_column(
        "transfer_optimization_runs",
        "run_status",
        type_=sa.String(length=64),
        existing_type=sa.String(length=9),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "transfer_optimization_runs",
        "run_status",
        type_=sa.String(length=9),
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "transfer_optimization_runs",
        "rules_status_snapshot",
        type_=sa.String(length=8),
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "rules_config_fields",
        "confidence",
        type_=sa.String(length=11),
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "rules_config",
        "rules_status",
        type_=sa.String(length=8),
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "rival_profiles",
        "overall_confidence",
        type_=sa.String(length=6),
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "rival_profiles",
        "derived_free_transfers_confidence",
        type_=sa.String(length=6),
        existing_type=sa.String(length=64),
        existing_nullable=True,
    )
    op.alter_column(
        "rival_gameweek_snapshots",
        "picks_availability",
        type_=sa.String(length=35),
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "recommendation_audits",
        "fpl_api_health_at_generation",
        type_=sa.String(length=8),
        existing_type=sa.String(length=64),
        existing_nullable=True,
    )
    op.alter_column(
        "recommendation_audits",
        "confidence",
        type_=sa.String(length=6),
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "raw_api_snapshots",
        "health_status",
        type_=sa.String(length=8),
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "position_segmentation_validation_runs",
        "run_status",
        type_=sa.String(length=9),
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "optimization_runs",
        "target_gameweek_status",
        type_=sa.String(length=18),
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "optimization_runs",
        "solver_status",
        type_=sa.String(length=10),
        existing_type=sa.String(length=64),
        existing_nullable=True,
    )
    op.alter_column(
        "optimization_runs",
        "run_status",
        type_=sa.String(length=9),
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "optimization_runs",
        "rules_status_snapshot",
        type_=sa.String(length=8),
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "model_versions",
        "model_family",
        type_=sa.String(length=32),
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "model_versions",
        "lifecycle_state",
        type_=sa.String(length=19),
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "model_promotions",
        "verdict",
        type_=sa.String(length=21),
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "manager_squad_snapshots",
        "source",
        type_=sa.String(length=12),
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "learning_signals",
        "signal_type",
        type_=sa.String(length=39),
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "learning_reports",
        "run_status",
        type_=sa.String(length=9),
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "leagues",
        "league_type",
        type_=sa.String(length=7),
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "league_sync_runs",
        "run_status",
        type_=sa.String(length=23),
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "league_sync_runs",
        "fpl_api_health_snapshot",
        type_=sa.String(length=8),
        existing_type=sa.String(length=64),
        existing_nullable=True,
    )
    op.alter_column(
        "ingestion_runs",
        "status",
        type_=sa.String(length=23),
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "ingestion_runs",
        "overall_health",
        type_=sa.String(length=8),
        existing_type=sa.String(length=64),
        existing_nullable=True,
    )
    op.alter_column(
        "h2h_strategy_candidates",
        "strategy_label",
        type_=sa.String(length=20),
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "h2h_simulation_runs",
        "run_status",
        type_=sa.String(length=9),
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "h2h_simulation_runs",
        "rules_status_snapshot",
        type_=sa.String(length=8),
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "h2h_simulation_runs",
        "confidence",
        type_=sa.String(length=6),
        existing_type=sa.String(length=64),
        existing_nullable=True,
    )
    op.alter_column(
        "failure_attributions",
        "reason_code",
        type_=sa.String(length=29),
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "failure_attributions",
        "category",
        type_=sa.String(length=20),
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "data_quality_issues",
        "severity",
        type_=sa.String(length=8),
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "counterfactual_results",
        "comparison_type",
        type_=sa.String(length=29),
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "cold_start_validation_runs",
        "run_status",
        type_=sa.String(length=9),
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "chip_recommendations",
        "verdict",
        type_=sa.String(length=16),
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "chip_optimization_runs",
        "run_status",
        type_=sa.String(length=9),
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "chip_optimization_runs",
        "rules_status_snapshot",
        type_=sa.String(length=8),
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "backtest_runs",
        "run_status",
        type_=sa.String(length=9),
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
