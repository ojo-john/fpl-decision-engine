"""Integration tests for the /push endpoints and app.notifications.push_sender against a real
database. The one real network boundary (pywebpush's own outbound HTTP call) is monkeypatched --
mirrors this repo's established "mock at the boundary, use a real DB" convention
(``patched_fpl_client``'s own docstring), never mocking the router/sender logic under test itself.

Every test uses its own uuid-suffixed endpoint (never a shared constant) -- this repo's own
established discipline for a shared, never-truncated Postgres (see conftest.py's own module
docstring): a fixed endpoint string reused across tests collides on ``push_subscriptions``'
unique ``endpoint`` constraint the moment one test doesn't clean up after itself.
"""

import uuid

from sqlalchemy import select

from packages.database.models.push_subscription import PushSubscription
from tests.integration.conftest import authed_client

_KEYS = {"p256dh": "test-p256dh-key", "auth": "test-auth-key"}


def _unique_endpoint() -> str:
    return f"https://push.example.com/v1/{uuid.uuid4().hex}"


class _StubVapid:
    """Stands in for py_vapid.Vapid02 in tests. Parsing a real PEM keypair is py_vapid's own
    concern, not this codebase's business logic -- and the configured VAPID_PRIVATE_KEY is
    deliberately an invalid placeholder outside a real deployment (see config.py's own docstring),
    so tests must not depend on a real key being configured. Only the per-subscription
    webpush()-calling/error-handling logic below is actually under test here."""

    @staticmethod
    def from_pem(pem: bytes) -> object:
        return object()


def _patch_vapid(monkeypatch) -> None:
    monkeypatch.setattr("app.notifications.push_sender.Vapid02", _StubVapid)


def test_subscribe_creates_a_row(db_session):
    endpoint = _unique_endpoint()
    client = authed_client()

    response = client.post("/push/subscribe", json={"endpoint": endpoint, "keys": _KEYS})

    assert response.status_code == 204
    row = db_session.execute(
        select(PushSubscription).where(PushSubscription.endpoint == endpoint)
    ).scalar_one()
    assert row.p256dh_key == _KEYS["p256dh"]
    assert row.auth_key == _KEYS["auth"]


def test_resubscribing_the_same_endpoint_upserts_not_duplicates(db_session):
    endpoint = _unique_endpoint()
    client = authed_client()
    client.post("/push/subscribe", json={"endpoint": endpoint, "keys": _KEYS})

    new_keys = {"p256dh": "rotated-p256dh-key", "auth": "rotated-auth-key"}
    response = client.post("/push/subscribe", json={"endpoint": endpoint, "keys": new_keys})

    assert response.status_code == 204
    rows = db_session.execute(select(PushSubscription).where(PushSubscription.endpoint == endpoint)).all()
    assert len(rows) == 1
    assert rows[0][0].p256dh_key == new_keys["p256dh"]


def test_unsubscribe_removes_the_row(db_session):
    endpoint = _unique_endpoint()
    client = authed_client()
    client.post("/push/subscribe", json={"endpoint": endpoint, "keys": _KEYS})

    response = client.request("DELETE", "/push/subscribe", json={"endpoint": endpoint, "keys": _KEYS})

    assert response.status_code == 204
    assert (
        db_session.execute(
            select(PushSubscription).where(PushSubscription.endpoint == endpoint)
        ).scalar_one_or_none()
        is None
    )


def test_unsubscribe_unknown_endpoint_is_a_no_op_not_an_error(db_session):
    client = authed_client()

    response = client.request(
        "DELETE", "/push/subscribe", json={"endpoint": _unique_endpoint(), "keys": _KEYS}
    )

    assert response.status_code == 204


def test_get_vapid_public_key_returns_the_configured_value(db_session):
    from packages.shared.config import get_settings

    client = authed_client()

    response = client.get("/push/vapid-public-key")

    assert response.status_code == 200
    assert response.json()["vapid_public_key"] == get_settings().vapid_public_key


def test_send_test_push_calls_webpush_once_per_subscription(db_session, monkeypatch):
    _patch_vapid(monkeypatch)
    client = authed_client()
    first_endpoint = _unique_endpoint()
    second_endpoint = _unique_endpoint()
    client.post("/push/subscribe", json={"endpoint": first_endpoint, "keys": _KEYS})
    client.post("/push/subscribe", json={"endpoint": second_endpoint, "keys": _KEYS})

    calls = []
    monkeypatch.setattr(
        "app.notifications.push_sender.webpush",
        lambda **kwargs: calls.append(kwargs) or None,
    )

    response = client.post("/push/test", json={"title": "t", "body": "b"})

    assert response.status_code == 200
    # >= not == : this shared-DB test suite may have other real subscriptions left by other
    # tests/runs -- what matters is webpush was really invoked once per row currently in the table.
    assert response.json()["sent_count"] >= 2
    assert len(calls) == response.json()["sent_count"]

    # A later run reusing these exact uuid4 endpoints is already effectively impossible, but
    # tidy up anyway rather than leaving permanent rows behind in the shared DB.
    db_session.query(PushSubscription).filter(
        PushSubscription.endpoint.in_([first_endpoint, second_endpoint])
    ).delete(synchronize_session=False)
    db_session.commit()


def test_send_push_to_all_deletes_subscription_on_410_gone(db_session, monkeypatch):
    from pywebpush import WebPushException

    from app.notifications.push_sender import send_push_to_all

    _patch_vapid(monkeypatch)
    endpoint = _unique_endpoint()
    db_session.add(PushSubscription(endpoint=endpoint, p256dh_key=_KEYS["p256dh"], auth_key=_KEYS["auth"]))
    db_session.commit()

    class _FakeResponse:
        status_code = 410

    def _raise_gone(**kwargs):
        raise WebPushException("gone", response=_FakeResponse())

    monkeypatch.setattr("app.notifications.push_sender.webpush", _raise_gone)

    send_push_to_all(db_session, title="t", body="b")

    assert (
        db_session.execute(
            select(PushSubscription).where(PushSubscription.endpoint == endpoint)
        ).scalar_one_or_none()
        is None
    )


def test_send_push_to_all_keeps_subscription_on_a_non_gone_failure(db_session, monkeypatch):
    from pywebpush import WebPushException

    from app.notifications.push_sender import send_push_to_all

    _patch_vapid(monkeypatch)
    endpoint = _unique_endpoint()
    db_session.add(PushSubscription(endpoint=endpoint, p256dh_key=_KEYS["p256dh"], auth_key=_KEYS["auth"]))
    db_session.commit()

    class _FakeResponse:
        status_code = 500

    def _raise_server_error(**kwargs):
        raise WebPushException("server error", response=_FakeResponse())

    monkeypatch.setattr("app.notifications.push_sender.webpush", _raise_server_error)

    send_push_to_all(db_session, title="t", body="b")

    row = db_session.execute(
        select(PushSubscription).where(PushSubscription.endpoint == endpoint)
    ).scalar_one_or_none()
    assert row is not None

    # Clean up: this row would otherwise sit in the shared DB forever (never touched by real
    # push logic again, since its endpoint is a random uuid no future test will ever reuse).
    db_session.delete(row)
    db_session.commit()
