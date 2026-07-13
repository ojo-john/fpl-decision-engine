"""manager squad snapshots table

Adds ``manager_squad_snapshots`` — Milestone 4 Layer 1's manager identity and
squad-state rows, populated by the ``MANAGER_SYNC`` job (real FPL manager
sync) or directly via ``POST /squads/manual`` (manual entry, no external
I/O). See ``packages/database/models/manager_squad_snapshot.py`` for the full
column docstrings and the insert-only/audit-snapshot design rationale.

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-13

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

from packages.domain.transfers import ManagerSquadSnapshotSource

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "manager_squad_snapshots",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("season_id", UUID(as_uuid=True), sa.ForeignKey("seasons.id"), nullable=False),
        sa.Column(
            "source",
            sa.Enum(
                ManagerSquadSnapshotSource,
                name="manager_squad_snapshot_source",
                native_enum=False,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column("fpl_manager_id", sa.Integer(), nullable=True),
        sa.Column("job_id", UUID(as_uuid=True), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column("as_of_gameweek_id", UUID(as_uuid=True), sa.ForeignKey("gameweeks.id"), nullable=True),
        sa.Column("bank", sa.Integer(), nullable=False),
        sa.Column("free_transfers_available", sa.Integer(), nullable=False),
        sa.Column("active_chip", sa.String(length=20), nullable=True),
        sa.Column("chips_used_json", JSONB(), nullable=True),
        sa.Column("overall_rank", sa.Integer(), nullable=True),
        sa.Column("total_points", sa.Integer(), nullable=True),
        sa.Column("squad_player_ids", JSONB(), nullable=False),
        sa.Column("squad_player_purchase_prices_json", JSONB(), nullable=False),
        sa.Column("squad_player_selling_prices_json", JSONB(), nullable=False),
        sa.Column(
            "estimated_purchase_price_player_ids_json",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "unresolved_fpl_element_ids_json", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("has_unresolved_players", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("raw_entry_json", JSONB(), nullable=True),
        sa.Column("raw_history_json", JSONB(), nullable=True),
        sa.Column("raw_picks_json", JSONB(), nullable=True),
        sa.Column("raw_transfers_json", JSONB(), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_manager_squad_snapshots_manager_is_current",
        "manager_squad_snapshots",
        ["fpl_manager_id"],
        unique=True,
        postgresql_where=sa.text("is_current AND source = 'manager_sync' AND fpl_manager_id IS NOT NULL"),
    )
    op.create_index("ix_manager_squad_snapshots_season_id", "manager_squad_snapshots", ["season_id"])


def downgrade() -> None:
    op.drop_index("ix_manager_squad_snapshots_season_id", table_name="manager_squad_snapshots")
    op.drop_index("ix_manager_squad_snapshots_manager_is_current", table_name="manager_squad_snapshots")
    op.drop_table("manager_squad_snapshots")
