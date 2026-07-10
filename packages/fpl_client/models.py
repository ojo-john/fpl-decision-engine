"""Result type returned by every FPLClient fetch method."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from packages.domain.ingestion import EndpointHealth


@dataclass
class FetchResult:
    """The outcome of one attempt to fetch one FPL API endpoint.

    Never raises on failure — every failure mode (network exception,
    non-2xx response) is represented here via ``health_status=BLOCKED`` and a
    sanitized ``error_message``, with ``raw_body=None``.
    """

    endpoint: str
    request_url: str
    request_params: dict[str, Any] | None
    retrieved_at: datetime
    http_status: int | None
    attempt_count: int
    latency_ms: int
    health_status: EndpointHealth
    checksum_sha256: str | None
    schema_signature: str | None
    raw_body: dict[str, Any] | list[Any] | None
    error_message: str | None
