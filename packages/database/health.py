"""Database connectivity health check."""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError


@dataclass
class HealthCheckResult:
    healthy: bool
    detail: str
    checked_at: datetime


def check_database_health(engine: Engine) -> HealthCheckResult:
    """Run a trivial ``SELECT 1`` against the given engine to verify connectivity.

    On failure, returns a *sanitized* error detail — only the exception's
    class name and a short safe message, never the raw connection string or
    any other potentially sensitive detail (e.g. host/credentials embedded
    in a DBAPI error message).
    """
    checked_at = datetime.now(UTC)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return HealthCheckResult(healthy=True, detail="ok", checked_at=checked_at)
    except SQLAlchemyError as exc:
        sanitized = _sanitize_error(exc)
        return HealthCheckResult(healthy=False, detail=sanitized, checked_at=checked_at)


def _sanitize_error(exc: SQLAlchemyError) -> str:
    """Build a safe error detail string: exception type + generic message only.

    Deliberately does NOT include ``str(exc)`` in full, since SQLAlchemy/DBAPI
    error messages frequently embed the connection URL (including
    credentials) or hostnames.
    """
    return f"{type(exc).__name__}: database connection failed"
