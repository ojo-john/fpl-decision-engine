"""Pydantic request/response schemas for the /push endpoints."""

from pydantic import BaseModel


class PushSubscriptionKeys(BaseModel):
    """Mirrors the real shape ``PushSubscription.toJSON().keys`` returns in the browser."""

    p256dh: str
    auth: str


class PushSubscribeRequest(BaseModel):
    endpoint: str
    keys: PushSubscriptionKeys


class VapidPublicKeyOut(BaseModel):
    vapid_public_key: str


class PushTestRequest(BaseModel):
    title: str = "FPL Decision Engine"
    body: str = "Test notification -- if you see this, push is working."


class PushTestOut(BaseModel):
    sent_count: int
