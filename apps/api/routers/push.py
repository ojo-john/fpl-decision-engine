"""POST /push/subscribe, DELETE /push/subscribe, GET /push/vapid-public-key, POST /push/test --
Web Push subscription management and a real end-to-end send verification endpoint.

Milestone 9 Layer 1 (proactive alerting). No per-user identity to key subscriptions against (see
``packages/database/models/push_subscription.py``'s own docstring) -- subscribe/unsubscribe are
keyed purely by the subscription's own ``endpoint``, upserted in place.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.notifications.push_sender import send_push_to_all
from apps.api.dependencies import get_db
from apps.api.schemas.push import (
    PushSubscribeRequest,
    PushTestOut,
    PushTestRequest,
    VapidPublicKeyOut,
)
from packages.database.models.push_subscription import PushSubscription
from packages.shared.config import get_settings

router = APIRouter(prefix="/push", tags=["push"])


@router.get("/vapid-public-key", response_model=VapidPublicKeyOut)
def get_vapid_public_key() -> VapidPublicKeyOut:
    """The VAPID public key is genuinely public by design (the browser embeds it directly in its
    own ``pushManager.subscribe()`` call) -- not build-time-baked into the frontend since Render
    env vars are the real source of truth, so the frontend fetches it here instead."""
    return VapidPublicKeyOut(vapid_public_key=get_settings().vapid_public_key)


@router.post("/subscribe", status_code=204, response_model=None)
def subscribe(body: PushSubscribeRequest, db: Session = Depends(get_db)) -> None:
    """Upsert by ``endpoint`` -- re-subscribing the same browser/device refreshes
    ``last_seen_at`` rather than creating a duplicate row."""
    existing = db.execute(
        select(PushSubscription).where(PushSubscription.endpoint == body.endpoint)
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if existing is not None:
        existing.p256dh_key = body.keys.p256dh
        existing.auth_key = body.keys.auth
        existing.last_seen_at = now
    else:
        db.add(
            PushSubscription(
                endpoint=body.endpoint,
                p256dh_key=body.keys.p256dh,
                auth_key=body.keys.auth,
            )
        )
    db.commit()


@router.delete("/subscribe", status_code=204, response_model=None)
def unsubscribe(body: PushSubscribeRequest, db: Session = Depends(get_db)) -> None:
    """Idempotent: removing an endpoint that was never subscribed (or already removed) is not an
    error -- the caller's own desired end state (not subscribed) is already true."""
    existing = db.execute(
        select(PushSubscription).where(PushSubscription.endpoint == body.endpoint)
    ).scalar_one_or_none()
    if existing is not None:
        db.delete(existing)
        db.commit()


@router.post("/test", response_model=PushTestOut)
def send_test_push(body: PushTestRequest, db: Session = Depends(get_db)) -> PushTestOut | JSONResponse:
    """Sends a REAL push to every subscribed device right now -- the concrete end-to-end
    verification step for this whole feature. Returns 200 with ``sent_count`` even if it's 0
    (no subscriptions exist yet); that's a legitimate state, not an error."""
    sent_count = send_push_to_all(db, title=body.title, body=body.body)
    return PushTestOut(sent_count=sent_count)
