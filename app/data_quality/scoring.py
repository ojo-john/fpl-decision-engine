"""Aggregates DataQualityIssues into a single score and an optimization go/no-go gate."""

from app.data_quality.models import DataQualityIssue
from packages.domain.ingestion import DataQualitySeverity, EndpointHealth
from packages.domain.rules import RulesStatus

CRITICAL_PENALTY, ERROR_PENALTY, WARNING_PENALTY = 25, 5, 1
MIN_SCORE_FOR_OPTIMIZATION = 70

_PENALTIES = {
    DataQualitySeverity.CRITICAL: CRITICAL_PENALTY,
    DataQualitySeverity.ERROR: ERROR_PENALTY,
    DataQualitySeverity.WARNING: WARNING_PENALTY,
    DataQualitySeverity.INFO: 0,
}


def compute_score(issues: list[DataQualityIssue]) -> float:
    """Compute a 0-100 data quality score from a flat penalty model."""
    penalty = sum(_PENALTIES[issue.severity] for issue in issues)
    return max(0.0, 100.0 - penalty)


def compute_optimization_permitted(
    score: float,
    issues: list[DataQualityIssue],
    rules_status: RulesStatus,
    overall_health: EndpointHealth,
) -> bool:
    """Decide whether downstream optimization is permitted to run against this ingestion.

    Any CRITICAL issue, a BLOCKED rules status, or BLOCKED overall endpoint
    health vetoes optimization outright regardless of score. DEGRADED never
    blocks by itself — it's surfaced in the report as a caution signal only.
    """
    if any(issue.severity == DataQualitySeverity.CRITICAL for issue in issues):
        return False
    if rules_status == RulesStatus.BLOCKED or overall_health == EndpointHealth.BLOCKED:
        return False
    return score >= MIN_SCORE_FOR_OPTIMIZATION
