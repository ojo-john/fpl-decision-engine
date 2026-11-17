"""Unit tests for apps.api.github_identity, using httpx.MockTransport (no real network)."""

import httpx
import pytest

from apps.api.github_identity import (
    GithubIdentityResolver,
    GithubIdentityUnavailableError,
    GithubTokenRejectedError,
    looks_like_github_token,
)

_TOKEN = "gho_exampletoken"


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _resolver(handler, clock=None) -> GithubIdentityResolver:
    return GithubIdentityResolver(transport=httpx.MockTransport(handler), clock=clock or _FakeClock())


def test_resolve_user_id_networkError_raisesUnavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(GithubIdentityUnavailableError):
        _resolver(handler).resolve_user_id(_TOKEN)


def test_resolve_user_id_timeout_raisesUnavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(GithubIdentityUnavailableError):
        _resolver(handler).resolve_user_id(_TOKEN)


@pytest.mark.parametrize("status", [401, 403])
def test_resolve_user_id_githubRejectsToken_raisesRejected(status):
    with pytest.raises(GithubTokenRejectedError):
        _resolver(lambda request: httpx.Response(status)).resolve_user_id(_TOKEN)


def test_resolve_user_id_github5xx_raisesUnavailable():
    with pytest.raises(GithubIdentityUnavailableError, match="status=502"):
        _resolver(lambda request: httpx.Response(502)).resolve_user_id(_TOKEN)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"login": "someone"}),
        httpx.Response(200, json={"id": "62852503"}),
        httpx.Response(200, json=["unexpected"]),
    ],
)
def test_resolve_user_id_malformedBody_raisesUnavailable(response):
    with pytest.raises(GithubIdentityUnavailableError):
        _resolver(lambda request: response).resolve_user_id(_TOKEN)


def test_resolve_user_id_validToken_returnsIdAndSendsBearer():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.github.com/user"
        assert request.headers["Authorization"] == f"Bearer {_TOKEN}"
        return httpx.Response(200, json={"id": 62852503, "login": "someone"})

    assert _resolver(handler).resolve_user_id(_TOKEN) == 62852503


def test_resolve_user_id_withinTtl_usesCacheWithoutCallingGithub():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"id": 1})

    resolver = _resolver(handler)
    resolver.resolve_user_id(_TOKEN)
    resolver.resolve_user_id(_TOKEN)

    assert len(calls) == 1


def test_resolve_user_id_afterTtlExpires_rechecksGithub():
    clock = _FakeClock()
    responses = iter([httpx.Response(200, json={"id": 1}), httpx.Response(401)])
    resolver = _resolver(lambda request: next(responses), clock=clock)
    resolver.resolve_user_id(_TOKEN)
    clock.now += 301

    with pytest.raises(GithubTokenRejectedError):
        resolver.resolve_user_id(_TOKEN)


def test_resolve_user_id_rejectedToken_isNotCached():
    responses = iter([httpx.Response(401), httpx.Response(200, json={"id": 7})])
    resolver = _resolver(lambda request: next(responses))
    with pytest.raises(GithubTokenRejectedError):
        resolver.resolve_user_id(_TOKEN)

    assert resolver.resolve_user_id(_TOKEN) == 7


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("gho_abc", True),
        ("ghp_abc", True),
        ("github_pat_abc", True),
        ("ghu_abc", True),
        ("ghs_abc", True),
        ("dev-token-change-me", False),
        ("", False),
        ("GHO_abc", False),
    ],
)
def test_looks_like_github_token_variousPrefixes_matchesOnlyGithubFormats(token, expected):
    assert looks_like_github_token(token) is expected
