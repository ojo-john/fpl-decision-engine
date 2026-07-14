"""The ``rival_gameweek_snapshots`` table: one row per attempted rival-picks fetch, insert-only.

Insert-only/audit-snapshot semantics — mirrors ``manager_squad_snapshots``'s
own justification (see that model's docstring): a rival's squad state is
real external state worth preserving per sync, and every attempt is worth
recording even when it fails, because the *absence* of data for a given
rival/gameweek is itself informative history for Layer 2's tendency analysis
(e.g. "this rival has gone private/unavailable for 3 straight gameweeks").
Unlike ``ManagerSquadSnapshot``, there is no ``is_current`` convention here —
Layer 2 reads the full history of attempts per ``(fpl_manager_id,
fpl_event_id)``, not just "the latest one", so rows are never superseded in
place.

Layer 2 compatibility note: ``app.transfers.free_transfers.derive_free_transfers_available``
(reused verbatim by Layer 2 for rival free-transfer derivation) takes plain
``current_rows``/``chip_rows`` shaped exactly like FPL's own
``history().current[]``/``history().chips[]``. This table deliberately does
NOT persist a rival's full transfer/chip history in some Layer-1-specific
shape — Layer 2 will need to fetch ``/entry/{id}/history/`` itself (this
table only stores one gameweek's ``picks/`` response per row) rather than
reconstructing history rows from here, since squeezing "the history the
free-transfer replay needs" out of a picks snapshot would require an
awkward translation. ``event_transfers``/``event_transfers_cost`` are stored
here as-is (verbatim from that gameweek's ``entry_history``) so a per-event
row still matches the same field names Layer 2 will see in
``history().current[]`` at a glance, even though the full-history replay
itself must still come from a fresh ``/history/`` fetch.

``captain_element_id``/``vice_captain_element_id`` store the RAW FPL element
id (unlike ``squad_player_ids``, which IS resolved to internal ``Player``
UUIDs) — captain identity is meaningful even for a player who no longer
resolves to a current-season ``Player`` row (e.g. transferred out of the
league mid-season in historical data), and Layer 2's tendency analysis over
captain choices only needs the raw identity for frequency counting, not the
resolved ``Player`` row.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.domain.h2h import PicksAvailability


class RivalGameweekSnapshot(Base):
    __tablename__ = "rival_gameweek_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    fpl_manager_id: Mapped[int] = mapped_column(Integer, nullable=False)
    fpl_event_id: Mapped[int] = mapped_column(Integer, nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)  # sync timestamp
    picks_availability: Mapped[PicksAvailability] = mapped_column(
        SAEnum(
            PicksAvailability,
            name="rival_gameweek_snapshot_picks_availability",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
    )
    http_status: Mapped[int | None] = mapped_column(Integer)
    active_chip: Mapped[str | None] = mapped_column(String(20))
    captain_element_id: Mapped[int | None] = mapped_column(Integer)  # raw FPL element id, not resolved
    vice_captain_element_id: Mapped[int | None] = mapped_column(Integer)  # raw FPL element id, not resolved
    squad_player_ids: Mapped[list | None] = mapped_column(
        JSONB
    )  # resolved internal Player UUIDs (as strings)
    unresolved_fpl_element_ids_json: Mapped[list | None] = mapped_column(JSONB)
    event_points: Mapped[int | None] = mapped_column(Integer)
    event_transfers: Mapped[int | None] = mapped_column(Integer)
    event_transfers_cost: Mapped[int | None] = mapped_column(Integer)
    overall_rank: Mapped[int | None] = mapped_column(Integer)
    raw_picks_json: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
