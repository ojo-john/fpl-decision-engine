"""Optimization-related domain enums.

Framework-agnostic (no SQLAlchemy/FastAPI imports here), matching the style
of ``packages/domain/jobs.py`` and ``packages/domain/rules.py``.
"""

from enum import Enum


class RecommendationType(str, Enum):
    """What kind of recommendation an ``optimization_runs`` row represents."""

    INITIAL_SQUAD = "initial_squad"
    STARTING_XI = "starting_xi"
    JOINT_SQUAD_AND_XI = "joint_squad_and_xi"


class OptimizationRunStatus(str, Enum):
    """Lifecycle status of an ``optimization_runs`` row."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class SolverStatus(str, Enum):
    """Outcome of the underlying MIP solve."""

    OPTIMAL = "optimal"
    INFEASIBLE = "infeasible"
    ERROR = "error"


class TargetGameweekStatus(str, Enum):
    """Whether a concrete target gameweek could be resolved for this run."""

    RESOLVED = "resolved"
    NO_ACTIVE_GAMEWEEK = "no_active_gameweek"
