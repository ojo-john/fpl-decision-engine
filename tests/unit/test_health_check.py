"""Tests for packages.database.health.check_database_health.

Verifies that a broken engine is reported as unhealthy with a populated,
but sanitized, detail message — one that never contains the raw connection
string / credentials that a real SQLAlchemy/DBAPI error would typically
include.
"""

from unittest.mock import MagicMock

from sqlalchemy.exc import OperationalError

from packages.database.health import check_database_health

FAKE_CONNECTION_STRING = "postgresql+psycopg://fpl:supersecretpassword@db.internal:5432/fpl_decision_engine"


def test_check_database_health_healthy():
    engine = MagicMock()
    conn = MagicMock()
    engine.connect.return_value.__enter__.return_value = conn

    result = check_database_health(engine)

    assert result.healthy is True
    assert result.detail
    assert result.checked_at is not None


def test_check_database_health_unhealthy_sanitizes_error():
    engine = MagicMock()
    error = OperationalError(
        FAKE_CONNECTION_STRING,
        None,
        Exception("connection refused"),
    )
    engine.connect.side_effect = error

    result = check_database_health(engine)

    assert result.healthy is False
    assert result.detail  # populated
    assert FAKE_CONNECTION_STRING not in result.detail
    assert "supersecretpassword" not in result.detail
    assert "OperationalError" in result.detail
