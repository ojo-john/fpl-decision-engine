"""SQLAlchemy ORM models.

Every model class lives here so ``migrations/env.py`` only needs one
whole-package import (``import packages.database.models``) to register all
tables on ``Base.metadata`` — future milestones adding a model only need to
update this file, not also remember to touch ``env.py``.
"""

from packages.database.models.data_quality_issue import DataQualityIssue
from packages.database.models.fixture import Fixture
from packages.database.models.gameweek import Gameweek
from packages.database.models.ingestion_run import IngestionRun
from packages.database.models.job import Job
from packages.database.models.manager_squad_snapshot import ManagerSquadSnapshot
from packages.database.models.model_metric import ModelMetric
from packages.database.models.model_version import ModelVersion
from packages.database.models.optimization_run import OptimizationRun
from packages.database.models.optimization_run_player import OptimizationRunPlayer
from packages.database.models.player import Player
from packages.database.models.player_features import PlayerFeatures
from packages.database.models.player_gameweek_stats import PlayerGameweekStats
from packages.database.models.position import Position
from packages.database.models.raw_api_snapshot import RawApiSnapshot
from packages.database.models.rules_config import RulesConfig
from packages.database.models.rules_config_field import RulesConfigField
from packages.database.models.season import Season
from packages.database.models.team import Team
from packages.database.models.transfer_optimization_run import TransferOptimizationRun
from packages.database.models.transfer_optimization_run_move import TransferOptimizationRunMove
from packages.database.models.transfer_optimization_run_scenario import TransferOptimizationRunScenario

__all__ = [
    "DataQualityIssue",
    "Fixture",
    "Gameweek",
    "IngestionRun",
    "Job",
    "ManagerSquadSnapshot",
    "ModelMetric",
    "ModelVersion",
    "OptimizationRun",
    "OptimizationRunPlayer",
    "Player",
    "PlayerFeatures",
    "PlayerGameweekStats",
    "Position",
    "RawApiSnapshot",
    "RulesConfig",
    "RulesConfigField",
    "Season",
    "Team",
    "TransferOptimizationRun",
    "TransferOptimizationRunMove",
    "TransferOptimizationRunScenario",
]
