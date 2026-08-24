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

    # Milestone 8 Layer 1: single-shared-secret auth for apps/web (a single-manager
    # system has no user table to authenticate against -- see apps.api.auth's own
    # docstring for why a shared token, not a full auth system, is the deliberate
    # choice here). Same "insecure dev default, must be overridden for anything
    # beyond local docker-compose" convention as application_secret_key above.
    app_shared_token: str = "dev-token-change-me"
    # Comma-separated allowed CORS origins (e.g. "http://localhost:3000,https://fpl.example.com").
    # A plain str, not list[str] -- pydantic-settings' env parsing for list-typed
    # fields expects JSON by default, which is more ceremony than a comma-separated
    # env var needs here. Empty by default: no browser origin is allowed until one
    # is explicitly configured (fail closed, not a wildcard).
    cors_allowed_origins: str = ""

    # Proactive push notifications (Web Push + VAPID) -- generate a real keypair with
    # `uv run vapid --gen` (from the py-vapid package, a pywebpush dependency) for anything beyond
    # local dev. vapid_public_key is genuinely public by design (the browser embeds it directly in
    # its subscribe() call) but still not build-time-baked into the frontend, since Render env vars
    # are the real source of truth -- the frontend fetches it from GET /push/vapid-public-key.
    # Unlike app_shared_token's placeholder-string default, this default must itself be a real,
    # valid EC P-256 PEM keypair -- Vapid02.from_pem() parses it eagerly (unit tests and any
    # environment without a configured real key construct a Vapid02 from whatever's here), so an
    # obviously-fake string like "dev-vapid-private-key-change-me" would crash every caller, not
    # just leave them insecure. This keypair is committed on purpose: it is a shared, public dev
    # fixture, never used for a real deployment (Render always sets a real one via env var).
    vapid_private_key: str = (
        "-----BEGIN PRIVATE KEY-----\n"
        "REDACTED-DEV-KEY\n"
        "REDACTED-DEV-KEY\n"
        "REDACTED-DEV-KEY\n"
        "-----END PRIVATE KEY-----\n"
    )
    vapid_public_key: str = (
        "BBv1f_y1TzCXNk7JhY3IReT_5GYY-d-PiW0Nm0t62NNGa4UJrPMxJnyJPDVHy-f5Iqi_WLRKz2R7_PQMXfxW0hw"
    )
    # Web Push spec requires a contact URI (mailto: or https:) in the VAPID JWT so a push service
    # can reach the sender about a misbehaving application server.
    vapid_subject: str = "mailto:dev@example.com"

    @property
    def cors_allowed_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_allowed_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    """Return a cached ``Settings`` instance.

    Cached so repeated calls (e.g. across modules) don't re-parse the
    environment / .env file on every access.
    """
    return Settings()
