"""Shared candidate shape fed into both MIP solvers."""

import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class OptimizationCandidate:
    """One eligible player considered by a solver.

    Only players with ``PlayerProjection.is_eligible=True`` (see
    ``app.optimization.projection``) ever become a candidate — availability
    is handled via candidate-pool membership, not a solver constraint.
    """

    player_id: uuid.UUID
    projected_points: float
    projection_source: str
    position_short_name: str
    team_id: uuid.UUID
    now_cost: int
