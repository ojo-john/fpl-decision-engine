"""Integration test: GET /health/db against a real database.

Requires a running Postgres with migrations applied (see conftest.py).
"""

from fastapi.testclient import TestClient

from apps.api.main import create_app


def test_health_db_returns_200_against_real_db(require_database):
    app = create_app()
    client = TestClient(app)
    response = client.get("/health/db")
    assert response.status_code == 200
