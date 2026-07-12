"""Resilient HTTP adapter over vaastav/Fantasy-Premier-League's historical CSV data.

Data source and licensing
--------------------------
Per-season historical data (teams/players/fixtures/per-gameweek stats) is
fetched from ``raw.githubusercontent.com/vaastav/Fantasy-Premier-League`` — a
long-running, actively-maintained (1,722+ stars as of verification), MIT
licensed *scraper* for Fantasy Premier League data. That repository's own
LICENSE file is explicit that the underlying FPL data itself remains
"property of fantasy.premierleague.com" — it is not a clean open-data grant.
This module (and everything built on it) treats this data exactly like this
project already treats the live FPL API: personal decision-support use only,
never redistribution. Credit: `@vaastav <https://github.com/vaastav>`_ for
the scraper/repackaging, and fantasy.premierleague.com for the underlying
data.

Conventions
-----------
Mirrors ``packages/fpl_client/client.py``'s conventions closely (timeout,
retry-with-backoff on connection errors/timeouts/429/5xx, never raises,
returns a ``FetchResult`` with health classification) but targets static CSV
files on GitHub rather than the live JSON FPL API. Reuses
``packages.fpl_client.health.classify_health``/``EndpointHealth`` (generic
over any HTTP response outcome) and ``packages.fpl_client.fingerprint``
(generic over any JSON-serializable body, and a parsed CSV — ``list[dict]`` —
is exactly that) directly rather than duplicating them. Parses CSV with the
stdlib ``csv.DictReader`` only — no ``pandas`` dependency here (that's
introduced in a later milestone layer, for modeling only).
"""

import csv
import io
import random
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from packages.domain.ingestion import EndpointHealth
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

BASE_URL = "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data"

# The four files fetched per season, in FK-dependency-friendly fetch order,
# paired with the "file stem" used to tag each file's raw_api_snapshots
# endpoint (e.g. "historical_2023-24_merged_gw").
SEASON_FILES: list[tuple[str, str]] = [
    ("teams.csv", "teams"),
    ("players_raw.csv", "players_raw"),
    ("fixtures.csv", "fixtures"),
    ("gws/merged_gw.csv", "merged_gw"),
]

# The earliest season with a modern, ingestible schema (position/team/xP
# columns present). 2016-17 through 2019-20 use an incompatible OPTA-derived
# schema and are permanently excluded — see the module docstring in
# app/ingestion/historical_normalization.py for the full schema-era rationale.
EARLIEST_USABLE_SEASON = "2020-21"


def _is_retryable_status(status_code: int) -> bool:
    return status_code in _RETRYABLE_STATUSES or status_code >= 500


def _sleep_with_jitter(attempt_index: int) -> None:
    """Sleep the backoff duration for the attempt just completed (0-indexed), with +/-20% jitter."""
    base = BACKOFF_SCHEDULE_S[min(attempt_index, len(BACKOFF_SCHEDULE_S) - 1)]
    jitter = base * JITTER_FRACTION
    time.sleep(max(0.0, base + random.uniform(-jitter, jitter)))


def _parse_csv(text: str) -> list[dict[str, Any]]:
    """Parse CSV text into a list of dicts. Stdlib csv.DictReader only — no pandas."""
    reader = csv.DictReader(io.StringIO(text))
    return list(reader)


class HistoricalFPLClient:
    """Wraps a single ``httpx.Client`` configured for raw.githubusercontent.com.

    Never raises: every failure mode (connection error, timeout, non-2xx
    response, invalid CSV body) is caught and turned into a ``FetchResult``
    with ``health_status=BLOCKED`` and a sanitized ``error_message``. Callers
    should always check ``FetchResult.raw_body``/``health_status`` rather
    than relying on exceptions.
    """

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

    def __enter__(self) -> "HistoricalFPLClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def fetch_season_file(self, season_label: str, file_path: str, file_stem: str) -> FetchResult:
        """Fetch and parse one CSV file for one season.

        ``endpoint`` is tagged ``historical_{season_label}_{file_stem}``
        (e.g. ``historical_2023-24_merged_gw``) so it can be persisted into
        the existing ``raw_api_snapshots`` table alongside live-API snapshots.
        """
        url = f"{BASE_URL}/{season_label}/{file_path}"
        endpoint_name = f"historical_{season_label}_{file_stem}"
        return self._get(endpoint_name, url)

    def fetch_all_season_files(self, season_label: str) -> dict[str, FetchResult]:
        """Fetch teams/players_raw/fixtures/merged_gw for one season, in that order.

        Returns a dict keyed by file stem (``"teams"``/``"players_raw"``/
        ``"fixtures"``/``"merged_gw"``).
        """
        return {
            file_stem: self.fetch_season_file(season_label, file_path, file_stem)
            for file_path, file_stem in SEASON_FILES
        }

    def _get(self, endpoint_name: str, url: str) -> FetchResult:
        attempt_count = 0
        last_error: str | None = None
        last_status: int | None = None
        start = time.monotonic()

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
                if 200 <= response.status_code < 300:
                    latency_ms = int((time.monotonic() - start) * 1000)
                    return self._success_result(endpoint_name, url, response, attempt_count, latency_ms)
                last_error = f"http_status_{response.status_code}"
                if not _is_retryable_status(response.status_code):
                    # Non-retryable 4xx (e.g. 404 - season/file doesn't exist): short-circuit.
                    break

            if attempt_count < MAX_ATTEMPTS:
                _sleep_with_jitter(attempt_count - 1)

        latency_ms = int((time.monotonic() - start) * 1000)
        health_status = classify_health(last_status, attempt_count, latency_ms)
        return FetchResult(
            endpoint=endpoint_name,
            request_url=url,
            request_params=None,
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
        self, endpoint_name: str, url: str, response: httpx.Response, attempt_count: int, latency_ms: int
    ) -> FetchResult:
        try:
            rows = _parse_csv(response.text)
        except (csv.Error, UnicodeDecodeError) as exc:
            return FetchResult(
                endpoint=endpoint_name,
                request_url=url,
                request_params=None,
                retrieved_at=datetime.now(UTC),
                http_status=response.status_code,
                attempt_count=attempt_count,
                latency_ms=latency_ms,
                health_status=EndpointHealth.BLOCKED,
                checksum_sha256=None,
                schema_signature=None,
                raw_body=None,
                error_message=f"invalid_csv: {type(exc).__name__}",
            )

        health_status = classify_health(response.status_code, attempt_count, latency_ms)
        return FetchResult(
            endpoint=endpoint_name,
            request_url=url,
            request_params=None,
            retrieved_at=datetime.now(UTC),
            http_status=response.status_code,
            attempt_count=attempt_count,
            latency_ms=latency_ms,
            health_status=health_status,
            checksum_sha256=sha256_hex(response.content),
            schema_signature=schema_signature(rows),
            raw_body=rows,
            error_message=None,
        )
