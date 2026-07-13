"""The ``manager_squad_snapshots`` table: one row per manager-squad-state observation.

Insert-only/audit-snapshot semantics — mirrors ``ingestion_runs``/
``model_versions``'s "each run is its own recordable event, never upserted in
place" convention, NOT the upsert-in-place convention used for
``player_gameweek_stats``/``player_features``. A manager's squad is real
external state that legitimately differs across sync times (transfers made,
price changes, chips played), and every sync is worth preserving for
audit/backtesting — unlike a player's actual points, which has exactly one
true value to converge on. So a fresh ``MANAGER_SYNC`` never updates a prior
row; it inserts a new one and marks the previous ``is_current`` row for that
``fpl_manager_id`` as superseded (see ``app/jobs/handlers/manager_sync.py``).

``MANUAL_ENTRY`` rows never participate in the ``is_current`` convention at
all (``is_current`` is always ``False`` for them) — they're addressed
directly by their own snapshot id by whatever created them, not looked up "as
the current one" for a manager, so a user can freely create many hypothetical
squads without any of them competing for a single current slot. This is also
why ``fpl_manager_id`` is nullable: a manual entry has no real FPL manager
behind it.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean as SABoolean
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func, text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.domain.transfers import ManagerSquadSnapshotSource


class ManagerSquadSnapshot(Base):
    __tablename__ = "manager_squad_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    season_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("seasons.id"), nullable=False
    )
    source: Mapped[ManagerSquadSnapshotSource] = mapped_column(
        SAEnum(
            ManagerSquadSnapshotSource,
            name="manager_squad_snapshot_source",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
    )
    # NULL for MANUAL_ENTRY — a manual squad has no real FPL manager behind it.
    fpl_manager_id: Mapped[int | None] = mapped_column(Integer)
    # The MANAGER_SYNC job that produced this row; NULL for manual entry (no job system involved).
    job_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("jobs.id"))
    as_of_gameweek_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("gameweeks.id")
    )
    bank: Mapped[int] = mapped_column(Integer, nullable=False)  # tenths-of-£m, same convention as now_cost
    free_transfers_available: Mapped[int] = mapped_column(Integer, nullable=False)
    active_chip: Mapped[str | None] = mapped_column(String(20))
    chips_used_json: Mapped[list | None] = mapped_column(JSONB)  # history().chips[] verbatim
    overall_rank: Mapped[int | None] = mapped_column(Integer)
    total_points: Mapped[int | None] = mapped_column(Integer)
    squad_player_ids: Mapped[list] = mapped_column(JSONB, nullable=False)  # list of UUIDs (as strings)
    squad_player_purchase_prices_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False
    )  # dict[str(player_id) -> int]
    squad_player_selling_prices_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False
    )  # dict[str(player_id) -> int]
    # Player ids (this season's internal UUIDs, as strings) whose purchase
    # price is a fallback estimate (now_cost, assuming zero unrealized
    # profit), not one actually derived from /transfers/ history.
    estimated_purchase_price_player_ids_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=[])
    # Raw FPL element ids from picks[] that didn't resolve to a known current-season Player.
    unresolved_fpl_element_ids_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=[])
    has_unresolved_players: Mapped[bool] = mapped_column(SABoolean, nullable=False, default=False)
    raw_entry_json: Mapped[dict | None] = mapped_column(JSONB)
    raw_history_json: Mapped[dict | None] = mapped_column(JSONB)
    raw_picks_json: Mapped[dict | None] = mapped_column(JSONB)
    raw_transfers_json: Mapped[list | None] = mapped_column(JSONB)
    is_current: Mapped[bool] = mapped_column(SABoolean, nullable=False, default=False)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index(
            "ix_manager_squad_snapshots_manager_is_current",
            "fpl_manager_id",
            unique=True,
            postgresql_where=text("is_current AND source = 'manager_sync' AND fpl_manager_id IS NOT NULL"),
        ),
        Index("ix_manager_squad_snapshots_season_id", "season_id"),
    )
