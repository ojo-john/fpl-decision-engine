"""The ``push_subscriptions`` table: one row per browser/device subscribed to Web Push alerts.

Milestone 9 Layer 1 (proactive alerting). This is a single-shared-secret system with no per-user
identity (see ``apps/web/lib/session.ts``'s own docstring: every session cookie is interchangeable
-- there's no session store, no user table). A push subscription therefore has no natural "owner"
beyond its own ``endpoint`` (the push service URL the browser's own ``PushManager.subscribe()``
call returns) -- this table is a flat list of this manager's own devices, notified as a group by
``app.notifications.push_sender.send_push_to_all``, not routed per-user.

Upsert-in-place on ``endpoint`` (mirrors ``teams``/``players``/``leagues``' current-state
convention, not an insert-only audit table) -- re-subscribing the same browser/device just
refreshes ``last_seen_at``, never creates a duplicate row.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # The push service URL PushManager.subscribe() returns -- unique per browser/device
    # registration, and the only real identity a subscription has in this single-manager system.
    endpoint: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    p256dh_key: Mapped[str] = mapped_column(String, nullable=False)
    auth_key: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
