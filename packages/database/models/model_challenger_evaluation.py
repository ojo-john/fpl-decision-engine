"""The ``model_challenger_evaluations`` table: one row per gameweek a shadow challenger is scored.

Milestone 7 Layer 5 (Recursive Learning Loop: Challenger Training/Shadow-Mode
Evaluation/Promotion Gates/Rollback). Populated by
``app.modeling.shadow_evaluation.compute_shadow_challenger_evaluation``, called
additively from the existing ``GAMEWEEK_REVIEW`` job
(``app.jobs.handlers.gameweek_review``) once a gameweek is known-finished.

Computed RETROACTIVELY, not logged prospectively during live optimization: by
the time a gameweek is finished, ``player_features`` already carries that
gameweek's real ``target_total_points`` label (Milestone 3 Layer 2) for every
player who was playable that gameweek. Both the current shadow challenger's
and the current champion's own artifacts are re-run against those same
already-persisted feature rows to get each model's prediction, compared
against the same real actuals — no new per-player-per-gameweek prospective
logging table is needed, and the shadow challenger never influences a single
live recommendation (it is scored purely after the fact).

``challenger_mae``/``champion_mae`` are computed over the SAME set of players
(the intersection of players either model could produce a prediction for that
gameweek) so the two numbers are directly comparable — ``n_observations`` is
that intersection's size, not either model's own individually-larger count.
Both are nullable only in the defensive case a row is later found to have lost
its comparability (never actually written that way by
``compute_shadow_challenger_evaluation`` itself, which returns ``None``
instead of persisting a row with zero comparable observations).

Effectively insert-only per ``(challenger_model_version_id, gameweek_id)``
pair (the ``UniqueConstraint`` below) — but ``compute_shadow_challenger_evaluation``
UPDATES an existing row in place on a GAMEWEEK_REVIEW rerun for the same
gameweek (e.g. after a late bonus-points correction changes the real
actuals), mirroring ``recommendation_outcomes``'s own "converges to one true
value" upsert convention rather than ``learning_signals``'s "every
computation is its own recordable event" convention — a re-scored gameweek
should converge to the corrected number, not accumulate stale duplicates that
would violate this table's own uniqueness constraint.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base


class ModelChallengerEvaluation(Base):
    __tablename__ = "model_challenger_evaluations"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    challenger_model_version_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("model_versions.id"), nullable=False
    )
    champion_model_version_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("model_versions.id"), nullable=False
    )
    season_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("seasons.id"), nullable=False
    )
    gameweek_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("gameweeks.id"), nullable=False
    )
    challenger_mae: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    champion_mae: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    n_observations: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "challenger_model_version_id",
            "gameweek_id",
            name="uq_model_challenger_evaluations_challenger_gameweek",
        ),
        Index("ix_model_challenger_evaluations_challenger_id", "challenger_model_version_id"),
        Index("ix_model_challenger_evaluations_gameweek_id", "gameweek_id"),
    )
