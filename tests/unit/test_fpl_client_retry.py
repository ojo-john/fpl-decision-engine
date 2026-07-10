"""Unit tests for FPLClient retry/backoff behavior, using httpx.MockTransport.

No real network access. Backoff sleeps are patched out so these tests run
fast regardless of the real [0.5s, 1.5s, 3.0s] schedule.
"""

import httpx
import pytest

from packages.domain.ingestion import EndpointHealth
from packages.fpl_client.client import FPLClient


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("packages.fpl_client.client.time.sleep", lambda *_args, **_kwargs: None)


def _client_with_handler(handler) -> FPLClient:
    return FPLClient(transport=httpx.MockTransport(handler))


def test_succeeds_on_first_attempt():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    client = _client_with_handler(handler)
    result = client.fetch_bootstrap_static()

    assert result.attempt_count == 1
    assert result.health_status == EndpointHealth.HEALTHY
    assert result.raw_body == {"ok": True}
    assert result.checksum_sha256 is not None
    assert result.schema_signature is not None
    assert result.error_message is None


def test_retries_on_500_then_succeeds():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] < 3:
            return httpx.Response(500)
        return httpx.Response(200, json={"ok": True})

    client = _client_with_handler(handler)
    result = client.fetch_bootstrap_static()

    assert calls["count"] == 3
    assert result.attempt_count == 3
    assert result.health_status == EndpointHealth.DEGRADED
    assert result.raw_body == {"ok": True}


def test_retries_on_429_then_succeeds():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] < 2:
            return httpx.Response(429)
        return httpx.Response(200, json={"ok": True})

    client = _client_with_handler(handler)
    result = client.fetch_bootstrap_static()

    assert calls["count"] == 2
    assert result.health_status == EndpointHealth.DEGRADED


def test_always_500_exhausts_retries_and_returns_blocked():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(500)

    client = _client_with_handler(handler)
    result = client.fetch_bootstrap_static()

    assert calls["count"] == 3
    assert result.attempt_count == 3
    assert result.health_status == EndpointHealth.BLOCKED
    assert result.raw_body is None
    assert result.error_message is not None


def test_non_retryable_404_short_circuits_without_wasted_retries():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(404)

    client = _client_with_handler(handler)
    result = client.fetch_bootstrap_static()

    assert calls["count"] == 1
    assert result.attempt_count == 1
    assert result.health_status == EndpointHealth.BLOCKED


def test_connection_error_retries_and_reports_blocked():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        raise httpx.ConnectError("boom", request=request)

    client = _client_with_handler(handler)
    result = client.fetch_bootstrap_static()

    assert calls["count"] == 3
    assert result.health_status == EndpointHealth.BLOCKED
    assert result.http_status is None
    assert "connection_error" in result.error_message


def test_timeout_retries_and_reports_blocked():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        raise httpx.ReadTimeout("timed out", request=request)

    client = _client_with_handler(handler)
    result = client.fetch_bootstrap_static()

    assert calls["count"] == 3
    assert result.health_status == EndpointHealth.BLOCKED
    assert "timeout" in result.error_message


def test_fetch_fixtures_with_event_param():
    seen_params = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_params.update(dict(request.url.params))
        return httpx.Response(200, json=[])

    client = _client_with_handler(handler)
    result = client.fetch_fixtures(event=1)

    assert seen_params == {"event": "1"}
    assert result.endpoint == "fixtures_by_event"


def test_fetch_fixtures_without_event_param():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    client = _client_with_handler(handler)
    result = client.fetch_fixtures()

    assert result.endpoint == "fixtures"


def test_fetch_event_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"leagues": "updated"})

    client = _client_with_handler(handler)
    result = client.fetch_event_status()

    assert result.endpoint == "event_status"
    assert result.raw_body == {"leagues": "updated"}
