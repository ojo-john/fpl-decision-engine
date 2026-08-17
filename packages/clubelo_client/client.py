"""Resilient adapter over http://api.clubelo.com -- real, free, no API key, confirmed live
(2026-08-17: ``GET /2026-08-17`` returns real current club Elo ratings as CSV).

Mirrors ``packages.fpl_client.client.FPLClient``'s resilience conventions exactly (explicit
connect/read timeouts, bounded retries with backoff+jitter on retryable statuses, never raises)
but returns a simpler ``ClubEloFetchResult`` -- ClubElo has no bootstrap-static-style schema
fingerprinting/health-classification needs, it's a single flat CSV endpoint.

ClubElo publishes no formal rate limit, but its own site asks callers to be reasonable (no bulk
scraping) -- ``MIN_REQUEST_INTERVAL_S`` enforces a floor between consecutive requests from one
client instance, in addition to the per-request retry/backoff below.
"""

import random
import time
from datetime import UTC, datetime
from typing import NamedTuple

import httpx

CONNECT_TIMEOUT_S = 5.0
READ_TIMEOUT_S = 15.0
MAX_ATTEMPTS = 3
BACKOFF_SCHEDULE_S = [0.5, 1.5, 3.0]
JITTER_FRACTION = 0.2

# Self-imposed floor between requests, out of respect for ClubElo's "be reasonable" ask -- not a
# documented hard limit, but 1/second is the conservative norm cited by other real integrations
# against this same API.
MIN_REQUEST_INTERVAL_S = 1.0

_BASE_URL = "http://api.clubelo.com"

# Non-2xx statuses worth retrying: 429 (rate-limited) and any 5xx.
_RETRYABLE_STATUSES = {429}


def _is_retryable_status(status_code: int) -> bool:
    return status_code in _RETRYABLE_STATUSES or status_code >= 500


def _sleep_with_jitter(attempt_index: int) -> None:
    base = BACKOFF_SCHEDULE_S[min(attempt_index, len(BACKOFF_SCHEDULE_S) - 1)]
    jitter = base * JITTER_FRACTION
    time.sleep(max(0.0, base + random.uniform(-jitter, jitter)))


class ClubEloFetchResult(NamedTuple):
    """``raw_csv`` is ``None`` on any failure -- callers must check it rather than relying on
    exceptions, exactly like ``packages.fpl_client``'s ``FetchResult.raw_body`` convention."""

    raw_csv: str | None
    http_status: int | None
    attempt_count: int
    error_message: str | None


class ClubEloClient:
    """Wraps a single ``httpx.Client`` configured for ``api.clubelo.com``. Never raises."""

    def __init__(
        self, timeout: httpx.Timeout | None = None, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._client = httpx.Client(
            timeout=timeout
            or httpx.Timeout(
                connect=CONNECT_TIMEOUT_S, read=READ_TIMEOUT_S, write=READ_TIMEOUT_S, pool=READ_TIMEOUT_S
            ),
            transport=transport,
        )
        self._last_request_monotonic: float | None = None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ClubEloClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def fetch_ratings_snapshot(self, as_of_date: str) -> ClubEloFetchResult:
        """All clubs' Elo ratings as of ``as_of_date`` (``YYYY-MM-DD``) -- current ratings for
        today's date, or a real historical snapshot for any past date (ClubElo's archive goes
        back decades; used both for live sync and for historical backtest validation)."""
        return self._get(f"{_BASE_URL}/{as_of_date}")

    def _throttle(self) -> None:
        if self._last_request_monotonic is None:
            return
        elapsed = time.monotonic() - self._last_request_monotonic
        remaining = MIN_REQUEST_INTERVAL_S - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def _get(self, url: str) -> ClubEloFetchResult:
        self._throttle()
        attempt_count = 0
        last_error: str | None = None
        last_status: int | None = None

        while attempt_count < MAX_ATTEMPTS:
            attempt_count += 1
            try:
                response = self._client.get(url)
            except httpx.TimeoutException as exc:
                last_error = f"timeout: {type(exc).__name__}"
                last_status = None
            except httpx.HTTPError as exc:
                last_error = f"connection_error: {type(exc).__name__}"
                last_status = None
            else:
                last_status = response.status_code
                self._last_request_monotonic = time.monotonic()
                if 200 <= response.status_code < 300:
                    return ClubEloFetchResult(
                        raw_csv=response.text,
                        http_status=response.status_code,
                        attempt_count=attempt_count,
                        error_message=None,
                    )
                last_error = f"http_status_{response.status_code}"
                if not _is_retryable_status(response.status_code):
                    break

            if attempt_count < MAX_ATTEMPTS:
                _sleep_with_jitter(attempt_count - 1)

        self._last_request_monotonic = time.monotonic()
        return ClubEloFetchResult(
            raw_csv=None, http_status=last_status, attempt_count=attempt_count, error_message=last_error
        )


def today_iso_date() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")
