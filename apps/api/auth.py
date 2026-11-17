"""Bearer-token auth dependency for every router except ``health.py``.

This is a single-manager system, not a multi-tenant one -- there is no user table to
authenticate against. Two credentials are accepted, both checked here and nowhere else:

1. ``APP_SHARED_TOKEN`` (``Settings.app_shared_token``), compared with
   ``secrets.compare_digest`` (never ``==``, which leaks timing information). This is what
   ``apps/web``'s server side sends on every backend call.
2. A GitHub access token (e.g. ``gh auth token``) belonging to a GitHub account whose numeric
   user id is listed in ``Settings.github_auth_allowed_user_ids``. Lets the owner (or a CLI
   agent acting for them) call the deployed API with an identity they already have, instead of
   copying the shared secret around. Disabled entirely while that allowlist is empty (the
   default), and a token is only ever sent to GitHub if it carries a GitHub token prefix -- see
   ``apps.api.github_identity``.

``health.py`` stays unauthenticated so infra/monitoring can always reach it. Fails closed: a
missing/wrong token is a 401; GitHub being unreachable while verifying a GitHub token is a 503,
never a silent pass-through.
"""

import logging
import secrets
from functools import lru_cache

from fastapi import Depends, Header, HTTPException

from apps.api.github_identity import (
    GithubIdentityResolver,
    GithubIdentityUnavailableError,
    GithubTokenRejectedError,
    looks_like_github_token,
)
from packages.shared.config import get_settings

logger = logging.getLogger(__name__)


@lru_cache
def get_github_identity_resolver() -> GithubIdentityResolver:
    """Process-wide resolver (owns the token cache). Overridable in tests via ``app.dependency_overrides``."""
    return GithubIdentityResolver()


def require_authorized_caller(
    authorization: str | None = Header(default=None),
    resolver: GithubIdentityResolver = Depends(get_github_identity_resolver),
) -> None:
    settings = get_settings()
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header.")

    token = authorization.removeprefix("Bearer ")
    if secrets.compare_digest(token, settings.app_shared_token):
        return

    allowed_user_ids = settings.github_auth_allowed_user_ids_set
    if not allowed_user_ids or not looks_like_github_token(token):
        raise HTTPException(status_code=401, detail="Invalid token.")

    _verify_github_token(token, allowed_user_ids, resolver)


def _verify_github_token(
    token: str, allowed_user_ids: frozenset[int], resolver: GithubIdentityResolver
) -> None:
    try:
        user_id = resolver.resolve_user_id(token)
    except GithubTokenRejectedError as exc:
        logger.info("auth_denied", extra={"auth_method": "github", "reason": "token_rejected"})
        raise HTTPException(status_code=401, detail="Invalid token.") from exc
    except GithubIdentityUnavailableError as exc:
        logger.warning("auth_denied", extra={"auth_method": "github", "reason": "github_unavailable"})
        raise HTTPException(
            status_code=503,
            detail="Could not verify GitHub identity right now; try again or use the shared token.",
        ) from exc

    if user_id not in allowed_user_ids:
        logger.info(
            "auth_denied",
            extra={"auth_method": "github", "reason": "user_not_allowlisted", "github_user_id": user_id},
        )
        raise HTTPException(status_code=403, detail="This GitHub account is not allowed.")
