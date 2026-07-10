"""Tests for packages.shared.config.Settings.

Uses ``_env_file=None`` when constructing Settings directly so these tests
are isolated from any real ``.env`` file that might exist in the repo root.
"""

import pytest
from pydantic import ValidationError

from packages.shared.config import Settings


def test_settings_loads_database_url_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost:5432/db")
    settings = Settings(_env_file=None)
    assert settings.database_url == "postgresql+psycopg://user:pass@localhost:5432/db"


def test_settings_missing_database_url_raises(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_optional_fpl_fields_default_to_none(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost:5432/db")
    monkeypatch.delenv("FPL_MANAGER_ID", raising=False)
    monkeypatch.delenv("FPL_H2H_LEAGUE_ID", raising=False)
    settings = Settings(_env_file=None)
    assert settings.fpl_manager_id is None
    assert settings.fpl_h2h_league_id is None
