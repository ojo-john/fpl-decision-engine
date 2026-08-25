"""Remotely dispatches this repo's own ``.github/workflows/train-production.yml`` via GitHub's
REST API -- the backend for the FPL dashboard's own "Retrain model" button.

Deliberately does NOT run ``MODEL_TRAINING`` in-process: that path is exactly what OOM-crashed
the live API on Render's free tier (512MB RAM) mid-training, a real incident -- see
``train-production.yml``'s own docstring for the full story. Training only ever runs on GitHub
Actions' own ``ubuntu-latest`` runners (~7GB RAM) now; this module's only job is to ask GitHub to
start that workflow, never to run any training work itself.
"""

import httpx

from packages.shared.config import get_settings

# Explicit timeouts on every leg -- this codebase's own standing principle (never rely on a
# client/SDK default). A single workflow_dispatch call to GitHub's API is a small, fast request;
# generous enough to absorb normal network variance without leaving a human waiting on a stuck
# button press for long.
_CONNECT_TIMEOUT_S = 5.0
_READ_TIMEOUT_S = 15.0

_WORKFLOW_FILE = "train-production.yml"
_DISPATCH_REF = "main"

# GitHub's workflow_dispatch endpoint returns 204 No Content on success, with no run id in the
# response -- the newly-created run has to be found separately (e.g. by listing runs), which this
# module deliberately doesn't attempt: the caller (a human watching the button) can just open the
# Actions tab, and correlating "which run is mine" synchronously here would be race-prone if more
# than one dispatch happens close together.
_SUCCESS_STATUS_CODE = 204


class GithubActionsDispatchError(Exception):
    """Raised when GitHub's REST API refuses or fails a ``workflow_dispatch`` request.

    Never retried here -- a human is waiting synchronously on this call (a button press in the
    FPL dashboard); surfacing a clear error immediately is more useful than silently retrying past
    the user's own patience. The caller (``POST /models/train-production``) turns this into an
    HTTP error response the button can display and let the user simply press again.
    """


def dispatch_train_production_workflow(transport: httpx.BaseTransport | None = None) -> None:
    """POSTs a ``workflow_dispatch`` event for ``train-production.yml`` on ``main``.

    ``transport`` is test-only (mirrors ``packages.clubelo_client.client.ClubEloClient``'s own
    injectable-transport precedent) -- real callers never pass it, exercising the real network via
    a one-off ``httpx.Client`` for this single call.

    Raises ``GithubActionsDispatchError`` on any transport failure or non-204 response (e.g. a
    missing/expired/under-scoped token, or the workflow file not existing on the target ref).
    """
    settings = get_settings()
    url = f"https://api.github.com/repos/{settings.github_repo}/actions/workflows/{_WORKFLOW_FILE}/dispatches"
    timeout = httpx.Timeout(_READ_TIMEOUT_S, connect=_CONNECT_TIMEOUT_S)
    try:
        with httpx.Client(transport=transport, timeout=timeout) as client:
            response = client.post(
                url,
                json={"ref": _DISPATCH_REF},
                headers={
                    "Authorization": f"Bearer {settings.github_actions_trigger_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
    except httpx.HTTPError as exc:
        raise GithubActionsDispatchError(f"Network error dispatching {_WORKFLOW_FILE}: {exc}") from exc

    if response.status_code != _SUCCESS_STATUS_CODE:
        raise GithubActionsDispatchError(
            f"GitHub Actions dispatch failed (status={response.status_code}): {response.text[:500]}"
        )
