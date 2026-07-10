"""Classifies the health of a single endpoint fetch attempt."""

from packages.domain.ingestion import EndpointHealth

DEGRADED_LATENCY_MS = 3000


def classify_health(http_status: int | None, attempt_count: int, latency_ms: int) -> EndpointHealth:
    """Classify a fetch outcome as HEALTHY, DEGRADED, or BLOCKED.

    BLOCKED: no usable HTTP status, or a non-2xx status.
    DEGRADED: succeeded (2xx) but only after a retry, or with high latency.
    HEALTHY: succeeded on the first attempt within the latency budget.
    """
    if http_status is None or not (200 <= http_status < 300):
        return EndpointHealth.BLOCKED
    if attempt_count > 1 or latency_ms > DEGRADED_LATENCY_MS:
        return EndpointHealth.DEGRADED
    return EndpointHealth.HEALTHY
