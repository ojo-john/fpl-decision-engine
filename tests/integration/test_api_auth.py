"""Integration tests for the shared-secret auth dependency + CORS (Milestone 8 Layer 1).

See ``apps.api.auth``'s own docstring for why a single shared token, not a
full auth system, is the deliberate choice for this single-manager system.
"""

from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.shared.config import get_settings
from tests.integration.conftest import authed_client


def test_protected_endpoint_without_authorization_header_returns_401():
    client = TestClient(create_app())

    response = client.get("/players")

    assert response.status_code == 401


def test_protected_endpoint_with_wrong_token_returns_401():
    client = TestClient(create_app())
    client.headers["Authorization"] = "Bearer definitely-not-the-real-token"

    response = client.get("/players")

    assert response.status_code == 401


def test_protected_endpoint_with_malformed_header_returns_401():
    client = TestClient(create_app())
    # Missing the "Bearer " scheme prefix.
    client.headers["Authorization"] = get_settings().app_shared_token

    response = client.get("/players")

    assert response.status_code == 401


def test_protected_endpoint_with_correct_token_is_not_rejected(require_database):
    client = authed_client()

    response = client.get("/players")

    # 200 either way (empty list pre-ingestion or a real list) -- the point of
    # this test is that auth itself doesn't block it, not the response body.
    assert response.status_code != 401


def test_health_endpoint_requires_no_authorization_header():
    client = TestClient(create_app())

    response = client.get("/health")

    assert response.status_code == 200


def test_cors_allows_a_configured_origin(monkeypatch):
    # Don't depend on whatever CORS_ALLOWED_ORIGINS happens to be set to in
    # this environment (CI sets no such env var at all, so the real
    # get_settings() here would return the empty default) -- override just
    # this test's app construction with a known allowlist.
    configured = get_settings().model_copy(update={"cors_allowed_origins": "http://localhost:3000"})
    monkeypatch.setattr("apps.api.main.get_settings", lambda: configured)
    client = TestClient(create_app())

    response = client.get("/health", headers={"Origin": "http://localhost:3000"})

    assert response.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_cors_rejects_an_unconfigured_origin():
    # No monkeypatch needed: whatever the real allowlist is in this
    # environment, "evil.example.com" is never in it.
    client = TestClient(create_app())

    response = client.get("/health", headers={"Origin": "http://evil.example.com"})

    assert "access-control-allow-origin" not in response.headers
