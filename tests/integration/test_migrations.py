"""Integration tests for Alembic migrations against a real Postgres database.

Requires a running Postgres (see conftest.py docstring). Runs the actual
`alembic upgrade head` / `alembic downgrade base` commands via subprocess so
this exercises exactly what CI and operators run.
"""

import subprocess
import sys
from pathlib import Path

from sqlalchemy import inspect

REPO_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_COLUMNS = {
    "id",
    "job_type",
    "requested_by",
    "trigger_type",
    "status",
    "parameters_json",
    "scheduled_for",
    "started_at",
    "completed_at",
    "attempt_number",
    "idempotency_key",
    "git_commit_sha",
    "application_version",
    "error_code",
    "error_message",
    "output_json",
    "logs_uri",
    "created_at",
    "updated_at",
}


def _run_alembic(*args: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        check=True,
    )


def test_upgrade_creates_jobs_table_with_expected_columns(require_database):
    from packages.database.session import engine

    _run_alembic("upgrade", "head")
    try:
        inspector = inspect(engine)
        assert "jobs" in inspector.get_table_names()
        columns = {col["name"] for col in inspector.get_columns("jobs")}
        assert EXPECTED_COLUMNS.issubset(columns)
    finally:
        _run_alembic("downgrade", "base")

    inspector = inspect(engine)
    assert "jobs" not in inspector.get_table_names()
