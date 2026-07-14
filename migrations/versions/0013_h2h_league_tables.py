"""h2h league tables

Adds ``leagues``/``league_entries``/``h2h_matches`` (upsert-in-place
current-state tables), ``rival_gameweek_snapshots`` (insert-only audit
snapshots, one row per attempted rival-picks fetch), and
``league_sync_runs`` (insert-only run-header) for Milestone 5 Layer 1 (H2H
Strategy foundations: league/match/rival-picks sync). See
``packages/database/models/league.py``, ``league_entry.py``,
``h2h_match.py``, ``rival_gameweek_snapshot.py``, and ``league_sync_run.py``
for the full column docstrings and design rationale.

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-13

"""

import sys
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

# Some alembic commands (e.g. `alembic history`) load version scripts
# without first running env.py (which normally inserts the repo root onto
# sys.path) — so this file adds the same path fixup defensively, letting it
# import `packages.*` regardless of which alembic command loads it.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from packages.domain.h2h import LeagueSyncRunStatus, LeagueType, PicksAvailability  # noqa: E402
from packages.domain.ingestion import EndpointHealth  # noqa: E402

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "leagues",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("fpl_league_id", sa.Integer(), nullable=False, unique=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column(
            "league_type",
            sa.Enum(LeagueType, name="league_type", native_enum=False, validate_strings=True),
            nullable=False,
        ),
        sa.Column("scoring", sa.String(length=20), nullable=True),
        sa.Column("admin_entry", sa.Integer(), nullable=True),
        sa.Column("start_event", sa.Integer(), nullable=True),
        sa.Column("ko_rounds", sa.Integer(), nullable=True),
        sa.Column("closed", sa.Boolean(), nullable=False),
        sa.Column("has_cup", sa.Boolean(), nullable=False),
        sa.Column("raw_league_json", JSONB(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "league_entries",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("league_id", UUID(as_uuid=True), sa.ForeignKey("leagues.id"), nullable=False),
        sa.Column("fpl_manager_id", sa.Integer(), nullable=False),
        sa.Column("player_name", sa.String(), nullable=True),
        sa.Column("entry_name", sa.String(), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("last_rank", sa.Integer(), nullable=True),
        sa.Column("matches_played", sa.Integer(), nullable=True),
        sa.Column("matches_won", sa.Integer(), nullable=True),
        sa.Column("matches_drawn", sa.Integer(), nullable=True),
        sa.Column("matches_lost", sa.Integer(), nullable=True),
        sa.Column("points_for", sa.Integer(), nullable=True),
        sa.Column("total", sa.Integer(), nullable=True),
        sa.Column("division", sa.Integer(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("league_id", "fpl_manager_id", name="uq_league_entries_league_manager"),
    )
    op.create_index("ix_league_entries_league_id", "league_entries", ["league_id"])

    op.create_table(
        "h2h_matches",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("fpl_match_id", sa.Integer(), nullable=False, unique=True),
        sa.Column("league_id", UUID(as_uuid=True), sa.ForeignKey("leagues.id"), nullable=False),
        sa.Column("event", sa.Integer(), nullable=False),
        sa.Column("entry_1_id", sa.Integer(), nullable=False),
        sa.Column("entry_2_id", sa.Integer(), nullable=False),
        sa.Column("entry_1_points", sa.Integer(), nullable=True),
        sa.Column("entry_2_points", sa.Integer(), nullable=True),
        sa.Column("entry_1_win", sa.Integer(), nullable=True),
        sa.Column("entry_1_draw", sa.Integer(), nullable=True),
        sa.Column("entry_1_loss", sa.Integer(), nullable=True),
        sa.Column("entry_2_win", sa.Integer(), nullable=True),
        sa.Column("entry_2_draw", sa.Integer(), nullable=True),
        sa.Column("entry_2_loss", sa.Integer(), nullable=True),
        sa.Column("winner", sa.Integer(), nullable=True),
        sa.Column("is_knockout", sa.Boolean(), nullable=False),
        sa.Column("knockout_name", sa.String(), nullable=True),
        sa.Column("is_bye", sa.Boolean(), nullable=False),
        sa.Column("seed_value", sa.Integer(), nullable=True),
        sa.Column("raw_match_json", JSONB(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_h2h_matches_league_id", "h2h_matches", ["league_id"])
    op.create_index("ix_h2h_matches_league_event", "h2h_matches", ["league_id", "event"])

    op.create_table(
        "rival_gameweek_snapshots",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("fpl_manager_id", sa.Integer(), nullable=False),
        sa.Column("fpl_event_id", sa.Integer(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "picks_availability",
            sa.Enum(
                PicksAvailability,
                name="rival_gameweek_snapshot_picks_availability",
                native_enum=False,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("active_chip", sa.String(length=20), nullable=True),
        sa.Column("captain_element_id", sa.Integer(), nullable=True),
        sa.Column("vice_captain_element_id", sa.Integer(), nullable=True),
        sa.Column("squad_player_ids", JSONB(), nullable=True),
        sa.Column("unresolved_fpl_element_ids_json", JSONB(), nullable=True),
        sa.Column("event_points", sa.Integer(), nullable=True),
        sa.Column("event_transfers", sa.Integer(), nullable=True),
        sa.Column("event_transfers_cost", sa.Integer(), nullable=True),
        sa.Column("overall_rank", sa.Integer(), nullable=True),
        sa.Column("raw_picks_json", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_rival_gameweek_snapshots_manager_event",
        "rival_gameweek_snapshots",
        ["fpl_manager_id", "fpl_event_id"],
    )

    op.create_table(
        "league_sync_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("job_id", UUID(as_uuid=True), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column("league_id", UUID(as_uuid=True), sa.ForeignKey("leagues.id"), nullable=True),
        sa.Column("season_id", UUID(as_uuid=True), sa.ForeignKey("seasons.id"), nullable=False),
        sa.Column(
            "run_status",
            sa.Enum(
                LeagueSyncRunStatus, name="league_sync_run_status", native_enum=False, validate_strings=True
            ),
            nullable=False,
            server_default=LeagueSyncRunStatus.RUNNING.value,
        ),
        sa.Column("rivals_synced_count", sa.Integer(), nullable=True),
        sa.Column("rivals_unavailable_count", sa.Integer(), nullable=True),
        sa.Column(
            "fpl_api_health_snapshot",
            sa.Enum(
                EndpointHealth,
                name="league_sync_run_fpl_api_health_snapshot",
                native_enum=False,
                validate_strings=True,
            ),
            nullable=True,
        ),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("league_sync_runs")

    op.drop_index("ix_rival_gameweek_snapshots_manager_event", table_name="rival_gameweek_snapshots")
    op.drop_table("rival_gameweek_snapshots")

    op.drop_index("ix_h2h_matches_league_event", table_name="h2h_matches")
    op.drop_index("ix_h2h_matches_league_id", table_name="h2h_matches")
    op.drop_table("h2h_matches")

    op.drop_index("ix_league_entries_league_id", table_name="league_entries")
    op.drop_table("league_entries")

    op.drop_table("leagues")
