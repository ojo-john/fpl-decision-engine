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
from packages.database.models.player import Player
from packages.database.models.position import Position
from packages.database.models.raw_api_snapshot import RawApiSnapshot
from packages.database.models.rules_config import RulesConfig
from packages.database.models.rules_config_field import RulesConfigField
from packages.database.models.season import Season
from packages.database.models.team import Team

__all__ = [
    "DataQualityIssue",
    "Fixture",
    "Gameweek",
    "IngestionRun",
    "Job",
    "Player",
    "Position",
    "RawApiSnapshot",
    "RulesConfig",
    "RulesConfigField",
    "Season",
    "Team",
]
