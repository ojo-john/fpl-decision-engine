"""Tests for the /health and /health/db endpoints, using FastAPI TestClient.

No real database is used: the `get_db` dependency is overridden with a stub
session bound to a lightweight in-memory SQLite engine (never actually
queried — no DB connection is opened), and `check_database_health` (imported
into the health router module) is monkeypatched to control the
healthy/unhealthy outcome.
"""

from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from apps.api.dependencies import get_db
from apps.api.main import create_app
from apps.api.routers import health as health_router_module
from packages.database.health import HealthCheckResult

_STUB_ENGINE = create_engine("sqlite://")


def _stub_get_db():
    session = MagicMock()
    session.get_bind.return_value = _STUB_ENGINE
    yield session


def _make_client():
    app = create_app()
    app.dependency_overrides[get_db] = _stub_get_db
    return TestClient(app)


def test_health_liveness_returns_200():
    client = _make_client()
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_db_returns_200_when_healthy(monkeypatch):
    monkeypatch.setattr(
        health_router_module,
        "check_database_health",
        lambda engine: HealthCheckResult(healthy=True, detail="ok", checked_at=None),
    )
    client = _make_client()
    response = client.get("/health/db")
    assert response.status_code == 200


def test_health_db_returns_503_when_unhealthy(monkeypatch):
    monkeypatch.setattr(
        health_router_module,
        "check_database_health",
        lambda engine: HealthCheckResult(
            healthy=False, detail="OperationalError: database connection failed", checked_at=None
        ),
    )
    client = _make_client()
    response = client.get("/health/db")
    assert response.status_code == 503
