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
    # Deliberately an invalid placeholder, not a usable dev keypair like app_shared_token's default:
    # a real PEM private key (even a throwaway one) must never be committed to source -- gitleaks
    # correctly flags that pattern regardless of intent. VAPID push therefore fails closed (a clear
    # ValueError from Vapid02.from_pem, not a silent wrong-looking success) until a real key is
    # configured via env var. Tests mock py_vapid's PEM parsing at the boundary instead of relying
    # on any configured key -- see tests/integration/test_api_push.py.
    vapid_private_key: str = "dev-vapid-private-key-change-me"
    vapid_public_key: str = "dev-vapid-public-key-change-me"
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
