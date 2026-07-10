"""Parametrized tests for packages.fpl_client.health.classify_health."""

import pytest

from packages.domain.ingestion import EndpointHealth
from packages.fpl_client.health import DEGRADED_LATENCY_MS, classify_health


@pytest.mark.parametrize(
    ("http_status", "attempt_count", "latency_ms", "expected"),
    [
        (200, 1, 100, EndpointHealth.HEALTHY),
        (200, 1, DEGRADED_LATENCY_MS, EndpointHealth.HEALTHY),
        (299, 1, 500, EndpointHealth.HEALTHY),
        (200, 2, 100, EndpointHealth.DEGRADED),
        (200, 3, 100, EndpointHealth.DEGRADED),
        (200, 1, DEGRADED_LATENCY_MS + 1, EndpointHealth.DEGRADED),
        (None, 1, 100, EndpointHealth.BLOCKED),
        (404, 1, 100, EndpointHealth.BLOCKED),
        (500, 3, 100, EndpointHealth.BLOCKED),
        (429, 1, 100, EndpointHealth.BLOCKED),
        (199, 1, 100, EndpointHealth.BLOCKED),
        (300, 1, 100, EndpointHealth.BLOCKED),
    ],
)
def test_classify_health(http_status, attempt_count, latency_ms, expected):
    assert classify_health(http_status, attempt_count, latency_ms) == expected
