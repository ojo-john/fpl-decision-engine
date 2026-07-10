"""Application settings, loaded from environment variables / .env file."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central application configuration.

    Values are sourced from environment variables first, then from a
    ``.env`` file if present (see ``.env.example`` at the repo root for the
    full list of supported variables and their meaning).
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    application_secret_key: str = "dev-secret-change-me"
    log_level: str = "INFO"
    environment: str = "development"

    # Public, non-secret FPL identifiers. Unused until a later milestone wires
    # up FPL API ingestion — kept here now as plain config placeholders.
    fpl_manager_id: str | None = None
    fpl_h2h_league_id: str | None = None


@lru_cache
def get_settings() -> Settings:
    """Return a cached ``Settings`` instance.

    Cached so repeated calls (e.g. across modules) don't re-parse the
    environment / .env file on every access.
    """
    return Settings()
