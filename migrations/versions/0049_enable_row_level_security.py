"""Enable Row-Level Security on every public table

Real incident (2026-09-01): Supabase's own automated security scanner flagged this project
CRITICAL -- every single public table had Row-Level Security disabled, meaning any of Supabase's
public PostgREST API roles (``anon``, ``authenticated``) could read, edit, or delete every row in
every table (including ``manager_squad_snapshots``, ``push_subscriptions``, ``rival_profiles``,
``learning_reports``) via the project's own auto-generated REST API, entirely independent of this
app's own FastAPI/``APP_SHARED_TOKEN`` authentication layer -- a completely separate, unguarded
access path this app never intended to expose.

Confirmed safe fix, not just "the obvious one": this app's own ``DATABASE_URL`` connects as the
``postgres`` role, which is both the table owner and a superuser in Supabase's default setup --
Postgres's own RLS model exempts the table owner from RLS restrictions unless
``FORCE ROW LEVEL SECURITY`` is also set (deliberately NOT set here). So ``ENABLE ROW LEVEL
SECURITY`` with zero policies attached does exactly what's needed: Supabase's ``anon``/
``authenticated``/``service_role`` PostgREST roles get zero access (no policy exists granting
them anything), while this app's own direct Postgres connection is completely unaffected --
verified live via ``select current_user`` returning ``postgres`` before writing this migration,
not assumed.

No policies are added anywhere -- this app has no legitimate use for the PostgREST API at all
(it only ever talks to Postgres directly via SQLAlchemy), so "zero access for everyone except the
owner" is the complete, correct fix, not a partial one needing follow-up policy work.

Table list is the real, live list of every public table this project had at the time this
migration was written (confirmed via ``pg_class``/``pg_namespace``, not guessed) -- a future new
table needs its own ``ENABLE ROW LEVEL SECURITY`` line in whichever migration creates it, the same
established convention this repo already has for e.g. enum column widths (see
``tests/unit/test_job_enums.py``) -- nothing automatically re-checks this for tables added later.

Revision ID: 0049
Revises: 0048
Create Date: 2026-09-01

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0049"
down_revision: str | None = "0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PUBLIC_TABLES = [
    "alembic_version",
    "backtest_runs",
    "chip_optimization_runs",
    "chip_recommendations",
    "chip_schedule_assignments",
    "chip_schedule_optimization_runs",
    "chip_window_candidates",
    "cold_start_validation_runs",
    "counterfactual_results",
    "data_quality_issues",
    "failure_attributions",
    "fixtures",
    "gameweeks",
    "h2h_matches",
    "h2h_simulation_runs",
    "h2h_strategy_candidates",
    "ingestion_runs",
    "jobs",
    "league_entries",
    "league_sync_runs",
    "leagues",
    "learning_reports",
    "learning_signals",
    "manager_squad_snapshots",
    "model_challenger_evaluations",
    "model_metrics",
    "model_promotions",
    "model_versions",
    "note_effectiveness_evaluations",
    "optimization_run_players",
    "optimization_runs",
    "player_alert_states",
    "player_availability_risk_notes",
    "player_features",
    "player_gameweek_stats",
    "player_price_history",
    "player_price_signal_observations",
    "player_role_notes",
    "players",
    "position_segmentation_validation_runs",
    "positions",
    "price_signal_calibrations",
    "push_subscriptions",
    "raw_api_snapshots",
    "recommendation_audits",
    "recommendation_outcomes",
    "rival_gameweek_snapshots",
    "rival_profiles",
    "rules_config",
    "rules_config_fields",
    "seasons",
    "start_probability_models",
    "teams",
    "transfer_optimization_run_moves",
    "transfer_optimization_run_scenarios",
    "transfer_optimization_runs",
]


def upgrade() -> None:
    for table_name in _PUBLIC_TABLES:
        op.execute(f'ALTER TABLE "{table_name}" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    for table_name in _PUBLIC_TABLES:
        op.execute(f'ALTER TABLE "{table_name}" DISABLE ROW LEVEL SECURITY')
