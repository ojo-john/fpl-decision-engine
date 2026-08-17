"""Unit tests for ClubEloClient retry/backoff behavior, using httpx.MockTransport.

No real network access. Backoff/throttle sleeps are patched out so these tests run fast
regardless of the real schedule -- mirrors tests/unit/test_fpl_client_retry.py's own precedent.
"""

import httpx
import pytest

from packages.clubelo_client.client import ClubEloClient


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("packages.clubelo_client.client.time.sleep", lambda *_args, **_kwargs: None)


def _client_with_handler(handler) -> ClubEloClient:
    return ClubEloClient(transport=httpx.MockTransport(handler))


def test_fetch_ratings_snapshot_succeeds_on_first_attempt():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="Rank,Club,Country,Level,Elo,From,To\n1,Arsenal,ENG,1,2000,,\n")

    client = _client_with_handler(handler)
    result = client.fetch_ratings_snapshot("2026-08-17")

    assert result.attempt_count == 1
    assert result.http_status == 200
    assert result.error_message is None
    assert "Arsenal" in result.raw_csv


def test_retries_on_500_then_succeeds():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] < 3:
            return httpx.Response(500)
        return httpx.Response(200, text="ok")

    client = _client_with_handler(handler)
    result = client.fetch_ratings_snapshot("2026-08-17")

    assert calls["count"] == 3
    assert result.attempt_count == 3
    assert result.raw_csv == "ok"


def test_non_retryable_404_short_circuits_immediately():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(404)

    client = _client_with_handler(handler)
    result = client.fetch_ratings_snapshot("not-a-real-date")

    assert calls["count"] == 1
    assert result.raw_csv is None
    assert result.http_status == 404


def test_exhausts_retries_and_reports_failure_on_persistent_500():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = _client_with_handler(handler)
    result = client.fetch_ratings_snapshot("2026-08-17")

    assert result.attempt_count == 3
    assert result.raw_csv is None
    assert result.error_message == "http_status_500"


def test_timeout_is_classified_and_never_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom", request=request)

    client = _client_with_handler(handler)
    result = client.fetch_ratings_snapshot("2026-08-17")

    assert result.raw_csv is None
    assert result.http_status is None
    assert "timeout" in result.error_message
