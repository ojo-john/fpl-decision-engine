"""Shared-secret auth dependency for apps/web (Milestone 8 Layer 1).

This is a single-manager system, not a multi-tenant one -- there is no user
table to authenticate against, and building one would be pure over-
engineering (see the top-level engineering standards: "no magic, no
cleverness", minimal surface for what the problem actually needs). A single
shared token, config-driven (``Settings.app_shared_token``, see
``packages.shared.config``), checked via ``secrets.compare_digest`` (never a
plain ``==``, which leaks timing information about how many leading
characters matched) is the deliberate, right-sized choice here.

Applied to every router except ``health.py`` (an unauthenticated health
check must stay reachable for infra/monitoring, mirroring why
``GET /health``/``GET /health/db`` have never required a DB-backed
``Job``/``JobStatus`` row the way every other endpoint does). Fails closed:
a missing or wrong token is a 401, never a silent pass-through.
"""

import secrets

from fastapi import Header, HTTPException

from packages.shared.config import get_settings


def require_shared_token(authorization: str | None = Header(default=None)) -> None:
    settings = get_settings()
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header.")

    token = authorization.removeprefix("Bearer ")
    if not secrets.compare_digest(token, settings.app_shared_token):
        raise HTTPException(status_code=401, detail="Invalid token.")
