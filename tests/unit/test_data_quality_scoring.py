"""Unit tests for app.data_quality.scoring: compute_score and compute_optimization_permitted."""

from app.data_quality.models import DataQualityIssue
from app.data_quality.scoring import (
    MIN_SCORE_FOR_OPTIMIZATION,
    compute_optimization_permitted,
    compute_score,
)
from packages.domain.ingestion import DataQualitySeverity, EndpointHealth
from packages.domain.rules import RulesStatus


def _issue(severity: DataQualitySeverity) -> DataQualityIssue:
    return DataQualityIssue(
        entity_type="player", entity_fpl_id=1, severity=severity, check_name="x", message="x"
    )


def test_compute_score_no_issues_is_100():
    assert compute_score([]) == 100.0


def test_compute_score_applies_penalties():
    issues = [_issue(DataQualitySeverity.ERROR), _issue(DataQualitySeverity.WARNING)]
    assert compute_score(issues) == 100.0 - 5 - 1


def test_compute_score_critical_penalty():
    issues = [_issue(DataQualitySeverity.CRITICAL)]
    assert compute_score(issues) == 75.0


def test_compute_score_info_no_penalty():
    issues = [_issue(DataQualitySeverity.INFO)]
    assert compute_score(issues) == 100.0


def test_compute_score_floors_at_zero():
    issues = [_issue(DataQualitySeverity.CRITICAL) for _ in range(10)]
    assert compute_score(issues) == 0.0


def test_optimization_blocked_by_any_critical_issue_even_with_high_score():
    issues = [_issue(DataQualitySeverity.CRITICAL)]
    assert (
        compute_optimization_permitted(
            score=100.0, issues=issues, rules_status=RulesStatus.OK, overall_health=EndpointHealth.HEALTHY
        )
        is False
    )


def test_optimization_blocked_by_rules_status_blocked():
    assert (
        compute_optimization_permitted(
            score=100.0, issues=[], rules_status=RulesStatus.BLOCKED, overall_health=EndpointHealth.HEALTHY
        )
        is False
    )


def test_optimization_blocked_by_overall_health_blocked():
    assert (
        compute_optimization_permitted(
            score=100.0, issues=[], rules_status=RulesStatus.OK, overall_health=EndpointHealth.BLOCKED
        )
        is False
    )


def test_optimization_permitted_when_score_at_threshold():
    assert (
        compute_optimization_permitted(
            score=MIN_SCORE_FOR_OPTIMIZATION,
            issues=[],
            rules_status=RulesStatus.OK,
            overall_health=EndpointHealth.HEALTHY,
        )
        is True
    )


def test_optimization_blocked_when_score_below_threshold():
    assert (
        compute_optimization_permitted(
            score=MIN_SCORE_FOR_OPTIMIZATION - 0.01,
            issues=[],
            rules_status=RulesStatus.OK,
            overall_health=EndpointHealth.HEALTHY,
        )
        is False
    )


def test_optimization_permitted_degraded_health_does_not_block():
    assert (
        compute_optimization_permitted(
            score=100.0, issues=[], rules_status=RulesStatus.OK, overall_health=EndpointHealth.DEGRADED
        )
        is True
    )


def test_optimization_permitted_degraded_rules_status_does_not_block():
    assert (
        compute_optimization_permitted(
            score=100.0, issues=[], rules_status=RulesStatus.DEGRADED, overall_health=EndpointHealth.HEALTHY
        )
        is True
    )
