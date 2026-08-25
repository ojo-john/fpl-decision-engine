"""Unit tests for app.integrations.github_actions, using httpx.MockTransport.

No real network access -- mirrors tests/unit/test_clubelo_client.py's own precedent for an
injectable-transport HTTP client.
"""

import httpx
import pytest

from app.integrations.github_actions import GithubActionsDispatchError, dispatch_train_production_workflow


def test_dispatch_succeeds_on_a_real_204_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path.endswith("/actions/workflows/train-production.yml/dispatches")
        assert request.headers["Authorization"].startswith("Bearer ")
        return httpx.Response(204)

    dispatch_train_production_workflow(transport=httpx.MockTransport(handler))


def test_dispatch_raises_on_a_non_204_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Bad credentials")

    with pytest.raises(GithubActionsDispatchError, match="status=401"):
        dispatch_train_production_workflow(transport=httpx.MockTransport(handler))


def test_dispatch_raises_on_a_network_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(GithubActionsDispatchError, match="Network error"):
        dispatch_train_production_workflow(transport=httpx.MockTransport(handler))
