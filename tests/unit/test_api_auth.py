"""Unit tests for apps.api.auth.require_authorized_caller.

The GitHub resolver is faked at its interface (``resolve_user_id``); its own HTTP behavior is
covered separately in test_github_identity.py. Settings are built with ``_env_file=None`` so a
real ``.env`` never leaks in.
"""

import pytest
from fastapi import HTTPException

from apps.api import auth
from apps.api.github_identity import GithubIdentityUnavailableError, GithubTokenRejectedError
from packages.shared.config import Settings

_SHARED = "shared-secret-value"
_ALLOWED_ID = 62852503
_GITHUB_TOKEN = "gho_exampletoken"


class _FakeResolver:
    """Returns a fixed user id, or raises a fixed error; records whether GitHub was consulted."""

    def __init__(self, user_id: int | None = _ALLOWED_ID, error: Exception | None = None) -> None:
        self._user_id = user_id
        self._error = error
        self.calls = 0

    def resolve_user_id(self, token: str) -> int:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._user_id


@pytest.fixture
def configure(monkeypatch):
    def _configure(allowed_ids: str = str(_ALLOWED_ID)) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/db")
        settings = Settings(
            _env_file=None, app_shared_token=_SHARED, github_auth_allowed_user_ids=allowed_ids
        )
        monkeypatch.setattr(auth, "get_settings", lambda: settings)

    return _configure


def _status_of(header: str | None, resolver: _FakeResolver) -> int | None:
    try:
        auth.require_authorized_caller(authorization=header, resolver=resolver)
    except HTTPException as exc:
        return exc.status_code
    return None


def test_require_authorized_caller_missingHeader_returns401(configure):
    configure()
    assert _status_of(None, _FakeResolver()) == 401


def test_require_authorized_caller_nonBearerScheme_returns401(configure):
    configure()
    assert _status_of(f"Basic {_SHARED}", _FakeResolver()) == 401


def test_require_authorized_caller_githubUnavailable_failsClosedWith503(configure):
    configure()
    resolver = _FakeResolver(error=GithubIdentityUnavailableError("down"))
    assert _status_of(f"Bearer {_GITHUB_TOKEN}", resolver) == 503


def test_require_authorized_caller_githubRejectsToken_returns401(configure):
    configure()
    resolver = _FakeResolver(error=GithubTokenRejectedError("bad"))
    assert _status_of(f"Bearer {_GITHUB_TOKEN}", resolver) == 401


def test_require_authorized_caller_githubUserNotAllowlisted_returns403(configure):
    configure()
    assert _status_of(f"Bearer {_GITHUB_TOKEN}", _FakeResolver(user_id=999)) == 403


def test_require_authorized_caller_emptyAllowlist_rejectsGithubTokenWithoutCallingGithub(configure):
    configure(allowed_ids="")
    resolver = _FakeResolver()
    status = _status_of(f"Bearer {_GITHUB_TOKEN}", resolver)
    assert (status, resolver.calls) == (401, 0)


def test_require_authorized_caller_wrongNonGithubToken_rejectedWithoutCallingGithub(configure):
    configure()
    resolver = _FakeResolver()
    status = _status_of("Bearer not-the-shared-secret", resolver)
    assert (status, resolver.calls) == (401, 0)


def test_require_authorized_caller_sharedToken_allowedWithoutCallingGithub(configure):
    configure()
    resolver = _FakeResolver()
    status = _status_of(f"Bearer {_SHARED}", resolver)
    assert (status, resolver.calls) == (None, 0)


def test_require_authorized_caller_allowlistedGithubUser_allowed(configure):
    configure(allowed_ids=f"123, {_ALLOWED_ID}")
    assert _status_of(f"Bearer {_GITHUB_TOKEN}", _FakeResolver()) is None


def test_github_auth_allowed_user_ids_set_malformedEntry_raises(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/db")
    settings = Settings(_env_file=None, github_auth_allowed_user_ids="123,ojo-john")
    with pytest.raises(ValueError):
        _ = settings.github_auth_allowed_user_ids_set
