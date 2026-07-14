"""rival profiles

Adds ``rival_profiles`` (upsert-in-place, current-state, keyed on
``(season_id, fpl_manager_id)``) for Milestone 5 Layer 2 (H2H Strategy:
rival-state derivation/prediction, computed by ``app.h2h.rival_state`` and
persisted by ``LEAGUE_SYNC``). See
``packages/database/models/rival_profile.py`` for the full column
docstrings and design rationale.

Revision ID: 0014
Revises: 0013
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

from packages.domain.h2h import RivalStateConfidence  # noqa: E402

# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rival_profiles",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("season_id", UUID(as_uuid=True), sa.ForeignKey("seasons.id"), nullable=False),
        sa.Column("fpl_manager_id", sa.Integer(), nullable=False),
        sa.Column("league_id", UUID(as_uuid=True), sa.ForeignKey("leagues.id"), nullable=True),
        sa.Column(
            "last_known_snapshot_id",
            UUID(as_uuid=True),
            sa.ForeignKey("rival_gameweek_snapshots.id"),
            nullable=True,
        ),
        sa.Column("derived_free_transfers_min", sa.Integer(), nullable=True),
        sa.Column("derived_free_transfers_max", sa.Integer(), nullable=True),
        sa.Column(
            "derived_free_transfers_confidence",
            sa.Enum(
                RivalStateConfidence,
                name="rival_profile_derived_free_transfers_confidence",
                native_enum=False,
                validate_strings=True,
            ),
            nullable=True,
        ),
        sa.Column("derived_likely_remaining_chips_json", JSONB(), nullable=True),
        sa.Column("predicted_no_transfer_probability", sa.Numeric(5, 4), nullable=True),
        sa.Column("predicted_one_transfer_probability", sa.Numeric(5, 4), nullable=True),
        sa.Column("predicted_multi_transfer_probability", sa.Numeric(5, 4), nullable=True),
        sa.Column("predicted_hit_probability", sa.Numeric(5, 4), nullable=True),
        sa.Column("predicted_captain_probabilities_json", JSONB(), nullable=True),
        sa.Column("predicted_likely_transfer_targets_json", JSONB(), nullable=True),
        sa.Column("tendency_features_json", JSONB(), nullable=True),
        sa.Column("unknowns_json", JSONB(), nullable=True),
        sa.Column("evidence_json", JSONB(), nullable=True),
        sa.Column(
            "overall_confidence",
            sa.Enum(
                RivalStateConfidence,
                name="rival_profile_overall_confidence",
                native_enum=False,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("season_id", "fpl_manager_id", name="uq_rival_profiles_season_manager"),
    )
    op.create_index("ix_rival_profiles_league_id", "rival_profiles", ["league_id"])


def downgrade() -> None:
    op.drop_index("ix_rival_profiles_league_id", table_name="rival_profiles")
    op.drop_table("rival_profiles")
