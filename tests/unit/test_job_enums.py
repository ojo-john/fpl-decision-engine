"""Guard the exact string values of JobType/JobStatus/TriggerType.

These values are persisted in the database and referenced by CI workflows
(e.g. ``--job-type system_health``), so accidental renames must fail loudly.
"""

from packages.domain.jobs import JobStatus, JobType, TriggerType

EXPECTED_JOB_TYPE_VALUES = {
    "RULES_SYNC": "rules_sync",
    "DATA_INGESTION": "data_ingestion",
    "HISTORICAL_DATA_INGESTION": "historical_data_ingestion",
    "MANAGER_SYNC": "manager_sync",
    "LEAGUE_SYNC": "league_sync",
    "TEAM_STRENGTH_SYNC": "team_strength_sync",
    "FEATURE_GENERATION": "feature_generation",
    "MODEL_TRAINING": "model_training",
    "START_PROBABILITY_MODEL_TRAINING": "start_probability_model_training",
    "PROJECTION_GENERATION": "projection_generation",
    "SQUAD_OPTIMIZATION": "squad_optimization",
    "LINEUP_OPTIMIZATION": "lineup_optimization",
    "TRANSFER_OPTIMIZATION": "transfer_optimization",
    "CHIP_SIMULATION": "chip_simulation",
    "H2H_SIMULATION": "h2h_simulation",
    "RECOMMENDATION_GENERATION": "recommendation_generation",
    "GAMEWEEK_REVIEW": "gameweek_review",
    "BACKTEST": "backtest",
    "COLD_START_PROJECTION_VALIDATION": "cold_start_projection_validation",
    "POSITION_SEGMENTATION_VALIDATION": "position_segmentation_validation",
    "FULL_PIPELINE": "full_pipeline",
    "SYSTEM_HEALTH": "system_health",
}

EXPECTED_JOB_STATUS_VALUES = {
    "QUEUED": "queued",
    "RUNNING": "running",
    "SUCCEEDED": "succeeded",
    "FAILED": "failed",
    "SKIPPED": "skipped",
    "CANCELLED": "cancelled",
}

EXPECTED_TRIGGER_TYPE_VALUES = {
    "LOCAL": "local",
    "API": "api",
    "WEB": "web",
    "GITHUB_MANUAL": "github_manual",
    "GITHUB_SCHEDULE": "github_schedule",
    "RETRY": "retry",
}


def test_job_type_values():
    assert {member.name: member.value for member in JobType} == EXPECTED_JOB_TYPE_VALUES


def test_job_status_values():
    assert {member.name: member.value for member in JobStatus} == EXPECTED_JOB_STATUS_VALUES


def test_trigger_type_values():
    assert {member.name: member.value for member in TriggerType} == EXPECTED_TRIGGER_TYPE_VALUES


# Real bug (found 2026-08-17): jobs.job_type/status/trigger_type are native_enum=False VARCHAR
# columns whose real width in an already-migrated database is frozen at whatever the enum looked
# like the FIRST TIME migration 0001 ran there -- NOT automatically kept in sync with later
# additions to these enums. Three job types added this session (32 chars each) silently exceeded
# the real dev database's frozen VARCHAR(25) job_type column -- every real invocation failed with
# a DB-level truncation error, invisible to a fresh test database (which auto-sizes the column
# against the CURRENT enum on every `alembic upgrade head`). See migration 0041, which widens all
# three columns to 50. This test is the guard against it happening again: fails loudly, in CI,
# against ANY database state, the moment a future enum member would exceed that width -- instead
# of silently succeeding in tests while failing for real in production.
_JOBS_TABLE_COLUMN_WIDTHS = {"job_type": 50, "status": 50, "trigger_type": 50}


def test_job_type_values_fit_the_real_jobs_table_column_width():
    max_len = max(len(member.value) for member in JobType)
    assert max_len <= _JOBS_TABLE_COLUMN_WIDTHS["job_type"], (
        f"JobType.{max((m for m in JobType), key=lambda m: len(m.value)).name} is {max_len} chars -- "
        f"exceeds jobs.job_type's real column width ({_JOBS_TABLE_COLUMN_WIDTHS['job_type']}). "
        "Add a migration widening the column before adding this member."
    )


def test_job_status_values_fit_the_real_jobs_table_column_width():
    max_len = max(len(member.value) for member in JobStatus)
    assert max_len <= _JOBS_TABLE_COLUMN_WIDTHS["status"]


def test_trigger_type_values_fit_the_real_jobs_table_column_width():
    max_len = max(len(member.value) for member in TriggerType)
    assert max_len <= _JOBS_TABLE_COLUMN_WIDTHS["trigger_type"]
