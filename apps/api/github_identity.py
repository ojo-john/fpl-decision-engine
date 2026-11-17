"""Resolves a GitHub access token to the numeric GitHub user id that owns it.

Lets an allowlisted GitHub account call this API with its own GitHub token (e.g. the output of
``gh auth token``) instead of the shared ``APP_SHARED_TOKEN`` -- see ``apps.api.auth`` for how
the result is used. Identity is the numeric user id, never the login: a GitHub login can be
renamed (and a freed-up login re-registered by someone else), the id cannot.

Caching decision: a verified token -> user id mapping is cached in-process for
``_CACHE_TTL_S``, keyed by the token's SHA-256 (the raw token is never held as a key). Without
it every API request would add a GitHub round trip (~100-300ms) and spend the caller's own
GitHub rate limit. Only successful lookups are cached; a rejected token is re-checked each time,
so revoking-then-reissuing never leaves a stale denial. A revoked token stays usable for at most
one TTL window -- the deliberate tradeoff for a single-user system.
"""

import hashlib
import logging
import threading
import time
from collections.abc import Callable

import httpx

logger = logging.getLogger(__name__)

_GITHUB_USER_URL = "https://api.github.com/user"
# Explicit timeouts -- never rely on httpx defaults (same values as app.integrations.github_actions).
_CONNECT_TIMEOUT_S = 5.0
_READ_TIMEOUT_S = 10.0
_CACHE_TTL_S = 300.0
# Bounds memory if many distinct valid tokens are ever presented; at the cap the cache is simply
# cleared (single-user system -- a smarter eviction policy would be unjustified complexity).
_CACHE_MAX_ENTRIES = 256

# Prefixes GitHub documents for its token types (OAuth app, classic PAT, fine-grained PAT,
# user-to-server, server-to-server). Only tokens that look like GitHub's are ever sent to GitHub,
# so a mistyped shared token is never leaked to a third party.
_GITHUB_TOKEN_PREFIXES = ("gho_", "ghp_", "github_pat_", "ghu_", "ghs_")


class GithubTokenRejectedError(Exception):
    """GitHub says the token is invalid, expired or revoked (HTTP 401/403). Not retryable."""


class GithubIdentityUnavailableError(Exception):
    """GitHub could not be reached or answered unexpectedly. Retryable by the caller.

    Callers must fail closed on this (deny the request), never fall through to "allowed".
    """


def looks_like_github_token(token: str) -> bool:
    return token.startswith(_GITHUB_TOKEN_PREFIXES)


class GithubIdentityResolver:
    """Token -> GitHub user id, with a small TTL cache. Thread-safe (FastAPI runs sync deps in a pool)."""

    def __init__(
        self,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._transport = transport
        self._clock = clock
        self._cache: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def resolve_user_id(self, token: str) -> int:
        """Return the GitHub user id owning ``token``.

        Raises ``GithubTokenRejectedError`` if GitHub rejects the token, or
        ``GithubIdentityUnavailableError`` on any network error, timeout, 5xx/unexpected status
        or malformed response body.
        """
        cache_key = hashlib.sha256(token.encode()).hexdigest()
        now = self._clock()
        with self._lock:
            cached = self._cache.get(cache_key)
        if cached is not None and cached[1] > now:
            return cached[0]

        user_id = self._fetch_user_id(token)
        with self._lock:
            if len(self._cache) >= _CACHE_MAX_ENTRIES:
                self._cache.clear()
            self._cache[cache_key] = (user_id, now + _CACHE_TTL_S)
        return user_id

    def _fetch_user_id(self, token: str) -> int:
        timeout = httpx.Timeout(_READ_TIMEOUT_S, connect=_CONNECT_TIMEOUT_S)
        started = time.monotonic()
        try:
            with httpx.Client(transport=self._transport, timeout=timeout) as client:
                response = client.get(
                    _GITHUB_USER_URL,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                )
        except httpx.HTTPError as exc:
            self._log_call(started, outcome="network_error")
            raise GithubIdentityUnavailableError(
                f"Network error calling GitHub /user: {type(exc).__name__}"
            ) from exc

        if response.status_code in (401, 403):
            self._log_call(started, outcome="rejected", status_code=response.status_code)
            raise GithubTokenRejectedError(f"GitHub rejected the token (status={response.status_code}).")
        if response.status_code != 200:
            self._log_call(started, outcome="unexpected_status", status_code=response.status_code)
            raise GithubIdentityUnavailableError(f"GitHub /user returned status={response.status_code}.")

        try:
            user_id = response.json()["id"]
        except (ValueError, KeyError, TypeError) as exc:
            self._log_call(started, outcome="malformed_body", status_code=200)
            raise GithubIdentityUnavailableError("GitHub /user returned a malformed body.") from exc
        if not isinstance(user_id, int):
            self._log_call(started, outcome="malformed_body", status_code=200)
            raise GithubIdentityUnavailableError("GitHub /user returned a non-integer id.")

        self._log_call(started, outcome="ok", status_code=200)
        return user_id

    @staticmethod
    def _log_call(started: float, outcome: str, status_code: int | None = None) -> None:
        # Structured log doubles as this codebase's metric signal (no metrics backend exists):
        # every external call records latency + outcome dimension. Never logs the token.
        logger.info(
            "github_identity_call",
            extra={
                "dependency": "github_user_api",
                "outcome": outcome,
                "status_code": status_code,
                "latency_ms": round((time.monotonic() - started) * 1000, 1),
            },
        )
