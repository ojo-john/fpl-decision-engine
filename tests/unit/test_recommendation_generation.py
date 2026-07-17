"""Unit tests for app.jobs.handlers.recommendation_generation's pure confidence/known-risks logic
(no DB/network) -- see tests/integration/test_recommendation_generation_job.py for the full,
DB-backed job behavior.
"""

import uuid
from decimal import Decimal

from app.jobs.handlers.recommendation_generation import (
    _COMPONENT_LABELS,
    compute_confidence,
    compute_known_risks,
)
from packages.domain.ingestion import EndpointHealth
from packages.domain.learning import RecommendationConfidence

_ALL_RESOLVED = {field: uuid.uuid4() for field in _COMPONENT_LABELS}
_NONE_RESOLVED: dict[str, uuid.UUID | None] = dict.fromkeys(_COMPONENT_LABELS)


class TestComputeConfidence:
    def test_compute_confidence_allComponentsHealthyHighQuality_returnsHigh(self):
        confidence, drivers = compute_confidence(4, EndpointHealth.HEALTHY, Decimal("95"))

        assert confidence == RecommendationConfidence.HIGH
        assert "resolved_components=4/4" in drivers
        assert "fpl_api_health=healthy" in drivers

    def test_compute_confidence_allComponentsHealthyAtThreshold_returnsHigh(self):
        confidence, _ = compute_confidence(4, EndpointHealth.HEALTHY, Decimal("80"))

        assert confidence == RecommendationConfidence.HIGH

    def test_compute_confidence_degradedHealth_returnsLow(self):
        confidence, drivers = compute_confidence(4, EndpointHealth.DEGRADED, Decimal("95"))

        assert confidence == RecommendationConfidence.LOW
        assert "fpl_api_health=degraded" in drivers

    def test_compute_confidence_blockedHealth_returnsLow(self):
        confidence, _ = compute_confidence(4, EndpointHealth.BLOCKED, Decimal("95"))

        assert confidence == RecommendationConfidence.LOW

    def test_compute_confidence_zeroComponentsResolved_returnsLow(self):
        confidence, drivers = compute_confidence(0, EndpointHealth.HEALTHY, Decimal("95"))

        assert confidence == RecommendationConfidence.LOW
        assert "resolved_components=0/4" in drivers

    def test_compute_confidence_oneComponentResolved_returnsLow(self):
        confidence, _ = compute_confidence(1, EndpointHealth.HEALTHY, Decimal("95"))

        assert confidence == RecommendationConfidence.LOW

    def test_compute_confidence_twoComponentsResolvedHealthy_returnsMedium(self):
        confidence, _ = compute_confidence(2, EndpointHealth.HEALTHY, Decimal("95"))

        assert confidence == RecommendationConfidence.MEDIUM

    def test_compute_confidence_threeComponentsResolvedHealthy_returnsMedium(self):
        confidence, _ = compute_confidence(3, EndpointHealth.HEALTHY, Decimal("95"))

        assert confidence == RecommendationConfidence.MEDIUM

    def test_compute_confidence_allComponentsHealthyBelowQualityThreshold_returnsMedium(self):
        confidence, _ = compute_confidence(4, EndpointHealth.HEALTHY, Decimal("79.99"))

        assert confidence == RecommendationConfidence.MEDIUM

    def test_compute_confidence_allComponentsHealthyMissingQualityScore_returnsMedium(self):
        confidence, drivers = compute_confidence(4, EndpointHealth.HEALTHY, None)

        assert confidence == RecommendationConfidence.MEDIUM
        assert "data_quality_score=unknown" in drivers

    def test_compute_confidence_noHealthDataAtAll_returnsLow(self):
        confidence, drivers = compute_confidence(4, None, None)

        assert confidence == RecommendationConfidence.LOW
        assert "fpl_api_health=unknown" in drivers


class TestComputeKnownRisks:
    def test_compute_known_risks_allComponentsResolvedHealthyWithChampion_returnsEmpty(self):
        risks = compute_known_risks(_ALL_RESOLVED, 5, EndpointHealth.HEALTHY, uuid.uuid4())

        assert risks == []

    def test_compute_known_risks_noComponentsResolved_mentionsAllFour(self):
        risks = compute_known_risks(_NONE_RESOLVED, 5, EndpointHealth.HEALTHY, uuid.uuid4())

        assert len(risks) == 4
        for label in _COMPONENT_LABELS.values():
            assert any(label in risk for risk in risks)

    def test_compute_known_risks_missingH2hComponent_mentionsGameweek(self):
        resolved = dict(_ALL_RESOLVED)
        resolved["h2h_simulation_run_id"] = None

        risks = compute_known_risks(resolved, 7, EndpointHealth.HEALTHY, uuid.uuid4())

        assert len(risks) == 1
        assert "GW7" in risks[0]
        assert "H2H simulation" in risks[0]

    def test_compute_known_risks_degradedHealth_mentionsDegraded(self):
        risks = compute_known_risks(_ALL_RESOLVED, 5, EndpointHealth.DEGRADED, uuid.uuid4())

        assert len(risks) == 1
        assert "DEGRADED" in risks[0]

    def test_compute_known_risks_noHealthData_omitsHealthRisk(self):
        risks = compute_known_risks(_ALL_RESOLVED, 5, None, uuid.uuid4())

        assert risks == []

    def test_compute_known_risks_noChampionModel_mentionsChampion(self):
        risks = compute_known_risks(_ALL_RESOLVED, 5, EndpointHealth.HEALTHY, None)

        assert len(risks) == 1
        assert "champion model" in risks[0]

    def test_compute_known_risks_everythingMissing_returnsAllSix(self):
        risks = compute_known_risks(_NONE_RESOLVED, 5, EndpointHealth.BLOCKED, None)

        assert len(risks) == 6
