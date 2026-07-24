"""Tests for app.jobs.registry: handler lookup for JobType values."""

import pytest

from app.jobs.handlers.backtest import BacktestJobRunner
from app.jobs.handlers.chip_simulation import ChipSimulationJobRunner
from app.jobs.handlers.data_ingestion import DataIngestionJobRunner
from app.jobs.handlers.feature_generation import FeatureGenerationJobRunner
from app.jobs.handlers.full_pipeline import FullPipelineJobRunner
from app.jobs.handlers.gameweek_review import GameweekReviewJobRunner
from app.jobs.handlers.h2h_simulation import H2HSimulationJobRunner
from app.jobs.handlers.historical_data_ingestion import HistoricalDataIngestionJobRunner
from app.jobs.handlers.league_sync import LeagueSyncJobRunner
from app.jobs.handlers.lineup_optimization import LineupOptimizationJobRunner
from app.jobs.handlers.manager_sync import ManagerSyncJobRunner
from app.jobs.handlers.model_training import ModelTrainingJobRunner
from app.jobs.handlers.recommendation_generation import RecommendationGenerationJobRunner
from app.jobs.handlers.rules_sync import RulesSyncJobRunner
from app.jobs.handlers.squad_optimization import SquadOptimizationJobRunner
from app.jobs.handlers.system_health import SystemHealthJobRunner
from app.jobs.handlers.transfer_optimization import TransferOptimizationJobRunner
from app.jobs.registry import JobHandlerNotImplementedError, get_handler
from packages.domain.jobs import JobType

_IMPLEMENTED_JOB_TYPES = {
    JobType.SYSTEM_HEALTH,
    JobType.RULES_SYNC,
    JobType.DATA_INGESTION,
    JobType.HISTORICAL_DATA_INGESTION,
    JobType.FEATURE_GENERATION,
    JobType.MODEL_TRAINING,
    JobType.SQUAD_OPTIMIZATION,
    JobType.LINEUP_OPTIMIZATION,
    JobType.MANAGER_SYNC,
    JobType.LEAGUE_SYNC,
    JobType.TRANSFER_OPTIMIZATION,
    JobType.H2H_SIMULATION,
    JobType.CHIP_SIMULATION,
    JobType.RECOMMENDATION_GENERATION,
    JobType.GAMEWEEK_REVIEW,
    JobType.BACKTEST,
    JobType.FULL_PIPELINE,
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


def test_model_training_resolves_to_its_runner():
    assert get_handler(JobType.MODEL_TRAINING) is ModelTrainingJobRunner


def test_manager_sync_resolves_to_its_runner():
    assert get_handler(JobType.MANAGER_SYNC) is ManagerSyncJobRunner


def test_transfer_optimization_resolves_to_its_runner():
    assert get_handler(JobType.TRANSFER_OPTIMIZATION) is TransferOptimizationJobRunner


def test_league_sync_resolves_to_its_runner():
    assert get_handler(JobType.LEAGUE_SYNC) is LeagueSyncJobRunner


def test_h2h_simulation_resolves_to_its_runner():
    assert get_handler(JobType.H2H_SIMULATION) is H2HSimulationJobRunner


def test_chip_simulation_resolves_to_its_runner():
    assert get_handler(JobType.CHIP_SIMULATION) is ChipSimulationJobRunner


def test_recommendation_generation_resolves_to_its_runner():
    assert get_handler(JobType.RECOMMENDATION_GENERATION) is RecommendationGenerationJobRunner


def test_gameweek_review_resolves_to_its_runner():
    assert get_handler(JobType.GAMEWEEK_REVIEW) is GameweekReviewJobRunner


def test_backtest_resolves_to_its_runner():
    assert get_handler(JobType.BACKTEST) is BacktestJobRunner


def test_full_pipeline_resolves_to_its_runner():
    assert get_handler(JobType.FULL_PIPELINE) is FullPipelineJobRunner


@pytest.mark.parametrize(
    "job_type",
    [jt for jt in JobType if jt not in _IMPLEMENTED_JOB_TYPES],
)
def test_unimplemented_job_types_raise(job_type):
    with pytest.raises(JobHandlerNotImplementedError):
        get_handler(job_type)
