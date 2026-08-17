"""SQLAlchemy ORM models.

Every model class lives here so ``migrations/env.py`` only needs one
whole-package import (``import packages.database.models``) to register all
tables on ``Base.metadata`` — future milestones adding a model only need to
update this file, not also remember to touch ``env.py``.
"""

from packages.database.models.backtest_run import BacktestRun
from packages.database.models.chip_optimization_run import ChipOptimizationRun
from packages.database.models.chip_recommendation import ChipRecommendation
from packages.database.models.chip_window_candidate import ChipWindowCandidate
from packages.database.models.cold_start_validation_run import ColdStartValidationRun
from packages.database.models.counterfactual_result import CounterfactualResult
from packages.database.models.data_quality_issue import DataQualityIssue
from packages.database.models.failure_attribution import FailureAttribution
from packages.database.models.fixture import Fixture
from packages.database.models.gameweek import Gameweek
from packages.database.models.h2h_match import H2HMatch
from packages.database.models.h2h_simulation_run import H2HSimulationRun
from packages.database.models.h2h_strategy_candidate import H2HStrategyCandidate
from packages.database.models.ingestion_run import IngestionRun
from packages.database.models.job import Job
from packages.database.models.league import League
from packages.database.models.league_entry import LeagueEntry
from packages.database.models.league_sync_run import LeagueSyncRun
from packages.database.models.learning_report import LearningReport
from packages.database.models.learning_signal import LearningSignal
from packages.database.models.manager_squad_snapshot import ManagerSquadSnapshot
from packages.database.models.model_challenger_evaluation import ModelChallengerEvaluation
from packages.database.models.model_metric import ModelMetric
from packages.database.models.model_promotion import ModelPromotion
from packages.database.models.model_version import ModelVersion
from packages.database.models.note_effectiveness_evaluation import NoteEffectivenessEvaluation
from packages.database.models.optimization_run import OptimizationRun
from packages.database.models.optimization_run_player import OptimizationRunPlayer
from packages.database.models.player import Player
from packages.database.models.player_availability_risk_note import PlayerAvailabilityRiskNote
from packages.database.models.player_features import PlayerFeatures
from packages.database.models.player_gameweek_stats import PlayerGameweekStats
from packages.database.models.player_price_history import PlayerPriceHistory
from packages.database.models.player_price_signal_observation import PlayerPriceSignalObservation
from packages.database.models.player_role_note import PlayerRoleNote
from packages.database.models.position import Position
from packages.database.models.price_signal_calibration import PriceSignalCalibration
from packages.database.models.raw_api_snapshot import RawApiSnapshot
from packages.database.models.recommendation_audit import RecommendationAudit
from packages.database.models.recommendation_outcome import RecommendationOutcome
from packages.database.models.rival_gameweek_snapshot import RivalGameweekSnapshot
from packages.database.models.rival_profile import RivalProfile
from packages.database.models.rules_config import RulesConfig
from packages.database.models.rules_config_field import RulesConfigField
from packages.database.models.season import Season
from packages.database.models.team import Team
from packages.database.models.transfer_optimization_run import TransferOptimizationRun
from packages.database.models.transfer_optimization_run_move import TransferOptimizationRunMove
from packages.database.models.transfer_optimization_run_scenario import TransferOptimizationRunScenario

__all__ = [
    "BacktestRun",
    "ChipOptimizationRun",
    "ChipRecommendation",
    "ChipWindowCandidate",
    "ColdStartValidationRun",
    "CounterfactualResult",
    "DataQualityIssue",
    "FailureAttribution",
    "Fixture",
    "Gameweek",
    "H2HMatch",
    "H2HSimulationRun",
    "H2HStrategyCandidate",
    "IngestionRun",
    "Job",
    "League",
    "LeagueEntry",
    "LeagueSyncRun",
    "LearningReport",
    "LearningSignal",
    "ManagerSquadSnapshot",
    "ModelChallengerEvaluation",
    "ModelMetric",
    "ModelPromotion",
    "ModelVersion",
    "NoteEffectivenessEvaluation",
    "OptimizationRun",
    "OptimizationRunPlayer",
    "Player",
    "PlayerAvailabilityRiskNote",
    "PlayerFeatures",
    "PlayerGameweekStats",
    "PlayerPriceHistory",
    "PlayerPriceSignalObservation",
    "PlayerRoleNote",
    "Position",
    "PriceSignalCalibration",
    "RawApiSnapshot",
    "RecommendationAudit",
    "RecommendationOutcome",
    "RivalGameweekSnapshot",
    "RivalProfile",
    "RulesConfig",
    "RulesConfigField",
    "Season",
    "Team",
    "TransferOptimizationRun",
    "TransferOptimizationRunMove",
    "TransferOptimizationRunScenario",
]
