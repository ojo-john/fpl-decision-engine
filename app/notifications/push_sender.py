"""Sends a Web Push notification to every subscribed browser/device.

Milestone 9 Layer 1. No per-user routing -- this is a single-shared-secret system with no
per-user identity (see ``packages/database/models/push_subscription.py``'s own docstring), so
every real alert (job failure, deadline reminder, price/injury change) goes to every subscribed
device via ``send_push_to_all``. Per-subscription failure isolation (mirrors
``app.jobs.handlers.league_sync``'s own per-rival isolation discipline): one dead subscription
must never stop the rest of a batch from being notified.
"""

import json
import logging

from py_vapid import Vapid02
from pywebpush import WebPushException, webpush
from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.database.models.push_subscription import PushSubscription
from packages.shared.config import get_settings

logger = logging.getLogger(__name__)

# Real, explicit timeout on every outbound push-service call -- this codebase's own standing
# principle (never rely on a client/SDK default), matches packages.fpl_client's own convention.
_PUSH_REQUEST_TIMEOUT_SECONDS = 10.0

_GONE_STATUS_CODE = 410


def send_push_to_all(session: Session, title: str, body: str, url: str | None = None) -> int:
    """Push ``title``/``body`` (and an optional ``url`` to open on click) to every subscribed
    device. Returns the count of subscriptions successfully notified.

    A subscription the push service reports as permanently gone (410 -- the browser itself
    unsubscribed or the registration expired) is deleted here rather than left to fail forever on
    every future call. Any other failure (network error, malformed subscription, push service
    outage) is logged and skipped -- never raised, so one bad subscription can't block the rest of
    a real alert from reaching every other device.
    """
    settings = get_settings()
    payload = json.dumps({"title": title, "body": body, "url": url})
    # VAPID_PRIVATE_KEY is stored as the full real PEM text (BEGIN/END lines included), not a
    # file path or a bare base64 string -- webpush()'s own string-form handling guesses between
    # several formats (file path vs. raw vs. DER), which is exactly the kind of ambiguity worth
    # avoiding for a real secret; constructing the Vapid02 object explicitly here is unambiguous.
    vapid = Vapid02.from_pem(settings.vapid_private_key.encode())

    subscriptions = session.execute(select(PushSubscription)).scalars().all()
    sent_count = 0
    for subscription in subscriptions:
        try:
            webpush(
                subscription_info={
                    "endpoint": subscription.endpoint,
                    "keys": {"p256dh": subscription.p256dh_key, "auth": subscription.auth_key},
                },
                data=payload,
                vapid_private_key=vapid,
                vapid_claims={"sub": settings.vapid_subject},
                timeout=_PUSH_REQUEST_TIMEOUT_SECONDS,
            )
            sent_count += 1
        except WebPushException as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code == _GONE_STATUS_CODE:
                logger.info(
                    "push_sender.subscription_gone_deleted",
                    extra={"push_subscription_id": str(subscription.id)},
                )
                session.delete(subscription)
                session.commit()
            else:
                logger.warning(
                    "push_sender.send_failed",
                    extra={
                        "push_subscription_id": str(subscription.id),
                        "http_status": status_code,
                        "error": str(exc),
                    },
                )
    return sent_count
