"""note_effectiveness_evaluations table

Adds ``note_effectiveness_evaluations`` -- the tracking/reporting half of the note-severity
feedback loop (the rubric half lives in ``apps.api.schemas.players.AvailabilityRiskNoteCreate``/
``RoleNoteCreate``'s own docstrings, not a migration). For every reviewed gameweek, for every
player who had an active ``player_availability_risk_notes``/``player_role_notes`` row applied to
their recommended-squad projection, records what severity was applied, what the projection was
with vs. without that note's own multiplier, what the player actually scored, and whether
applying the note moved the projection closer to or further from that real outcome. See
``packages/database/models/note_effectiveness_evaluation.py`` for the full column docstrings and
design rationale, and ``app/learning/note_effectiveness.py`` for the computation this table
persists.

Revision ID: 0033
Revises: 0032
Create Date: 2026-08-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "note_effectiveness_evaluations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "recommendation_outcome_id",
            UUID(as_uuid=True),
            sa.ForeignKey("recommendation_outcomes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("player_id", UUID(as_uuid=True), sa.ForeignKey("players.id"), nullable=False),
        sa.Column("note_type", sa.String(20), nullable=False),
        sa.Column(
            "availability_risk_note_id",
            UUID(as_uuid=True),
            sa.ForeignKey("player_availability_risk_notes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "role_boost_note_id",
            UUID(as_uuid=True),
            sa.ForeignKey("player_role_notes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("note_severity", sa.Numeric(4, 2), nullable=False),
        sa.Column("projected_points_with_note", sa.Numeric(6, 2), nullable=False),
        sa.Column("projected_points_without_note", sa.Numeric(6, 2), nullable=False),
        sa.Column("actual_points", sa.Numeric(6, 2), nullable=False),
        sa.Column("error_with_note", sa.Numeric(6, 2), nullable=False),
        sa.Column("error_without_note", sa.Numeric(6, 2), nullable=False),
        sa.Column("moved_closer_to_actual", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_unique_constraint(
        "uq_note_effectiveness_evaluations_outcome_player_type",
        "note_effectiveness_evaluations",
        ["recommendation_outcome_id", "player_id", "note_type"],
    )
    op.create_index(
        "ix_note_effectiveness_evaluations_outcome_id",
        "note_effectiveness_evaluations",
        ["recommendation_outcome_id"],
    )
    op.create_index(
        "ix_note_effectiveness_evaluations_player_id", "note_effectiveness_evaluations", ["player_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_note_effectiveness_evaluations_player_id", table_name="note_effectiveness_evaluations"
    )
    op.drop_index(
        "ix_note_effectiveness_evaluations_outcome_id", table_name="note_effectiveness_evaluations"
    )
    op.drop_constraint(
        "uq_note_effectiveness_evaluations_outcome_player_type",
        "note_effectiveness_evaluations",
        type_="unique",
    )
    op.drop_table("note_effectiveness_evaluations")
