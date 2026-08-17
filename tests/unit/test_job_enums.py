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
