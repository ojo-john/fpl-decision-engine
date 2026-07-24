"""The ``leagues`` table: one row per FPL H2H league we sync, upserted in place.

Upsert-in-place/current-state semantics (mirrors ``teams``/``players``, NOT
``manager_squad_snapshots``'s insert-only/audit convention) — a league's own
metadata (name, admin, scoring format) has exactly one true current value to
converge on, there is no value in preserving every historical variant of it.
``scoring``/``admin_entry`` are stored verbatim from the FPL API (``scoring``
is a raw single-letter code like ``"h"``, not reinterpreted into our own
enum) — only ``league_type`` is our own classification, and even that is
just a record of which endpoint family this row came from (see
``packages.domain.h2h.LeagueType``'s own docstring), not something parsed
out of the raw response.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean as SABoolean
from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.domain.h2h import LeagueType


class League(Base):
    __tablename__ = "leagues"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    fpl_league_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    league_type: Mapped[LeagueType] = mapped_column(
        SAEnum(LeagueType, name="league_type", native_enum=False, validate_strings=True),
        nullable=False,
    )
    scoring: Mapped[str | None] = mapped_column(String(20))  # raw verbatim (e.g. "h"), not reinterpreted
    admin_entry: Mapped[int | None] = mapped_column(Integer)  # raw FPL manager id of the league admin
    start_event: Mapped[int | None] = mapped_column(Integer)
    ko_rounds: Mapped[int | None] = mapped_column(Integer)
    closed: Mapped[bool] = mapped_column(SABoolean, nullable=False)
    has_cup: Mapped[bool] = mapped_column(SABoolean, nullable=False)
    # Exactly one row should have this True at a time -- same application-enforced (no DB
    # constraint), "unset every other row before setting the new one" invariant this codebase
    # already uses for seasons.is_current/model_versions.is_champion. The Rivals page defaults
    # to whichever league has this flag; a manager can sync several leagues but only one is
    # "the" one shown without an explicit tab switch. The first league ever synced becomes
    # priority automatically (see app.jobs.handlers.league_sync); every one after that stays
    # non-priority until POST /leagues/{league_id}/priority is called explicitly.
    is_priority: Mapped[bool] = mapped_column(SABoolean, nullable=False, default=False)
    raw_league_json: Mapped[dict | None] = mapped_column(JSONB)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
