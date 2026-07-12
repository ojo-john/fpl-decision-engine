"""Tests for app.jobs.registry: handler lookup for JobType values."""

import pytest

from app.jobs.handlers.data_ingestion import DataIngestionJobRunner
from app.jobs.handlers.feature_generation import FeatureGenerationJobRunner
from app.jobs.handlers.historical_data_ingestion import HistoricalDataIngestionJobRunner
from app.jobs.handlers.lineup_optimization import LineupOptimizationJobRunner
from app.jobs.handlers.rules_sync import RulesSyncJobRunner
from app.jobs.handlers.squad_optimization import SquadOptimizationJobRunner
from app.jobs.handlers.system_health import SystemHealthJobRunner
from app.jobs.registry import JobHandlerNotImplementedError, get_handler
from packages.domain.jobs import JobType

_IMPLEMENTED_JOB_TYPES = {
    JobType.SYSTEM_HEALTH,
    JobType.RULES_SYNC,
    JobType.DATA_INGESTION,
    JobType.HISTORICAL_DATA_INGESTION,
    JobType.FEATURE_GENERATION,
    JobType.SQUAD_OPTIMIZATION,
    JobType.LINEUP_OPTIMIZATION,
}


def test_system_health_resolves_to_its_runner():
    assert get_handler(JobType.SYSTEM_HEALTH) is SystemHealthJobRunner


def test_rules_sync_resolves_to_its_runner():
    assert get_handler(JobType.RULES_SYNC) is RulesSyncJobRunner


def test_data_ingestion_resolves_to_its_runner():
    assert get_handler(JobType.DATA_INGESTION) is DataIngestionJobRunner


def test_historical_data_ingestion_resolves_to_its_runner():
    assert get_handler(JobType.HISTORICAL_DATA_INGESTION) is HistoricalDataIngestionJobRunner


def test_feature_generation_resolves_to_its_runner():
    assert get_handler(JobType.FEATURE_GENERATION) is FeatureGenerationJobRunner


def test_squad_optimization_resolves_to_its_runner():
    assert get_handler(JobType.SQUAD_OPTIMIZATION) is SquadOptimizationJobRunner


def test_lineup_optimization_resolves_to_its_runner():
    assert get_handler(JobType.LINEUP_OPTIMIZATION) is LineupOptimizationJobRunner


@pytest.mark.parametrize(
    "job_type",
    [jt for jt in JobType if jt not in _IMPLEMENTED_JOB_TYPES],
)
def test_unimplemented_job_types_raise(job_type):
    with pytest.raises(JobHandlerNotImplementedError):
        get_handler(job_type)
