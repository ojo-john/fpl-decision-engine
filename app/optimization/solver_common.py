"""Shared helper for mapping PuLP solver statuses onto our SolverStatus domain enum."""

import pulp

from packages.domain.optimization import SolverStatus

_STATUS_MAP = {
    "Optimal": SolverStatus.OPTIMAL,
    "Infeasible": SolverStatus.INFEASIBLE,
    "Undefined": SolverStatus.INFEASIBLE,
}


def map_solver_status(pulp_status: int) -> SolverStatus:
    """Map a ``pulp.LpProblem.status`` code to our ``SolverStatus`` domain enum."""
    label = pulp.LpStatus[pulp_status]
    return _STATUS_MAP.get(label, SolverStatus.ERROR)
