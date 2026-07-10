"""Thin, resilient HTTP adapter over the public FPL API.

``FPLClient`` never raises: every failure mode (connection error, timeout,
non-2xx response) is caught and turned into a ``FetchResult`` with
``health_status=BLOCKED`` and a sanitized ``error_message``. Callers should
always check ``FetchResult.raw_body``/``health_status`` rather than relying
on exceptions.
"""

import random
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from packages.domain.ingestion import EndpointHealth
from packages.fpl_client.endpoints import BOOTSTRAP_STATIC_URL, EVENT_STATUS_URL, FIXTURES_URL
from packages.fpl_client.fingerprint import schema_signature, sha256_hex
from packages.fpl_client.health import classify_health
from packages.fpl_client.models import FetchResult

CONNECT_TIMEOUT_S = 5.0
READ_TIMEOUT_S = 15.0
MAX_ATTEMPTS = 3
BACKOFF_SCHEDULE_S = [0.5, 1.5, 3.0]
JITTER_FRACTION = 0.2

# Non-2xx statuses worth retrying: 429 (rate-limited) and any 5xx.
_RETRYABLE_STATUSES = {429}


def _is_retryable_status(status_code: int) -> bool:
    return status_code in _RETRYABLE_STATUSES or status_code >= 500


def _sleep_with_jitter(attempt_index: int) -> None:
    """Sleep the backoff duration for the attempt just completed (0-indexed), with +/-20% jitter."""
    base = BACKOFF_SCHEDULE_S[min(attempt_index, len(BACKOFF_SCHEDULE_S) - 1)]
    jitter = base * JITTER_FRACTION
    time.sleep(max(0.0, base + random.uniform(-jitter, jitter)))


class FPLClient:
    """Wraps a single ``httpx.Client`` configured for the FPL public API."""

    def __init__(
        self, timeout: httpx.Timeout | None = None, transport: httpx.BaseTransport | None = None
    ) -> None:
        # ``transport`` is exposed only so tests can inject an
        # ``httpx.MockTransport`` without any real network access.
        self._client = httpx.Client(
            timeout=timeout
            or httpx.Timeout(
                connect=CONNECT_TIMEOUT_S, read=READ_TIMEOUT_S, write=READ_TIMEOUT_S, pool=READ_TIMEOUT_S
            ),
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "FPLClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def fetch_bootstrap_static(self) -> FetchResult:
        return self._get("bootstrap_static", BOOTSTRAP_STATIC_URL, params=None)

    def fetch_fixtures(self, event: int | None = None) -> FetchResult:
        params = {"event": event} if event is not None else None
        endpoint_name = "fixtures_by_event" if event is not None else "fixtures"
        return self._get(endpoint_name, FIXTURES_URL, params=params)

    def fetch_event_status(self) -> FetchResult:
        return self._get("event_status", EVENT_STATUS_URL, params=None)

    def _get(self, endpoint_name: str, url: str, params: dict[str, Any] | None) -> FetchResult:
        attempt_count = 0
        last_error: str | None = None
        last_status: int | None = None
        start = time.monotonic()

        while attempt_count < MAX_ATTEMPTS:
            attempt_count += 1
            try:
                response = self._client.get(url, params=params)
            except httpx.TimeoutException as exc:
                last_error = f"timeout: {type(exc).__name__}"
                last_status = None
            except httpx.HTTPError as exc:
                last_error = f"connection_error: {type(exc).__name__}"
                last_status = None
            else:
                last_status = response.status_code
                if 200 <= response.status_code < 300:
                    latency_ms = int((time.monotonic() - start) * 1000)
                    return self._success_result(
                        endpoint_name, url, params, response, attempt_count, latency_ms
                    )
                last_error = f"http_status_{response.status_code}"
                if not _is_retryable_status(response.status_code):
                    # Non-retryable 4xx (e.g. 404): short-circuit immediately.
                    break

            if attempt_count < MAX_ATTEMPTS:
                _sleep_with_jitter(attempt_count - 1)

        latency_ms = int((time.monotonic() - start) * 1000)
        health_status = classify_health(last_status, attempt_count, latency_ms)
        return FetchResult(
            endpoint=endpoint_name,
            request_url=url,
            request_params=params,
            retrieved_at=datetime.now(UTC),
            http_status=last_status,
            attempt_count=attempt_count,
            latency_ms=latency_ms,
            health_status=health_status,
            checksum_sha256=None,
            schema_signature=None,
            raw_body=None,
            error_message=last_error,
        )

    def _success_result(
        self,
        endpoint_name: str,
        url: str,
        params: dict[str, Any] | None,
        response: httpx.Response,
        attempt_count: int,
        latency_ms: int,
    ) -> FetchResult:
        try:
            body = response.json()
        except ValueError as exc:
            return FetchResult(
                endpoint=endpoint_name,
                request_url=url,
                request_params=params,
                retrieved_at=datetime.now(UTC),
                http_status=response.status_code,
                attempt_count=attempt_count,
                latency_ms=latency_ms,
                health_status=EndpointHealth.BLOCKED,
                checksum_sha256=None,
                schema_signature=None,
                raw_body=None,
                error_message=f"invalid_json: {type(exc).__name__}",
            )

        health_status = classify_health(response.status_code, attempt_count, latency_ms)
        return FetchResult(
            endpoint=endpoint_name,
            request_url=url,
            request_params=params,
            retrieved_at=datetime.now(UTC),
            http_status=response.status_code,
            attempt_count=attempt_count,
            latency_ms=latency_ms,
            health_status=health_status,
            checksum_sha256=sha256_hex(response.content),
            schema_signature=schema_signature(body),
            raw_body=body,
            error_message=None,
        )
