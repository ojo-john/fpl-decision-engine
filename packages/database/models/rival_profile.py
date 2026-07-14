"""The ``rival_profiles`` table: one row per (season, rival), upserted in place.

Milestone 5 Layer 2 (H2H Strategy: rival-state derivation/prediction). Unlike
``rival_gameweek_snapshots`` (insert-only, one row per attempted picks fetch,
preserved forever as raw observation history), this table holds exactly one
*current* row per rival — the latest computed DERIVED + PREDICTED state,
recomputed and upserted in place every ``LEAGUE_SYNC`` run (mirrors
``league_entries``'s own upsert-in-place rationale: there is one current
truth for "what do we currently believe about this rival", not a sequence of
observations worth preserving individually — the *inputs* to that belief are
what ``rival_gameweek_snapshots``/``fetch_manager_history`` already preserve).

Keyed on ``(season_id, fpl_manager_id)``, not also ``league_id`` — a rival's
derived tendencies are a property of the manager/season, not of which shared
league we happened to compute them from. ``league_id`` is retained purely as
provenance (which league's ``LEAGUE_SYNC`` run last computed this profile),
nullable because a rival could theoretically be shared across leagues with no
single canonical one.

Every ``derived_*``/``predicted_*``/``tendency_features_json`` field here is
computed by the pure ``app.h2h.rival_state.compute_rival_state`` function —
this model itself contains no computation, only persistence. See that
module's docstring for the full confidence-assignment and cold-start-handling
rationale (thin/single-snapshot rivals — the realistic starting state for
every real rival right now — must degrade honestly to ``LOW`` confidence and
a non-empty ``unknowns_json``, never a falsely-precise ``HIGH``/``MEDIUM``).
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, UniqueConstraint, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.domain.h2h import RivalStateConfidence


class RivalProfile(Base):
    __tablename__ = "rival_profiles"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    season_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("seasons.id"), nullable=False
    )
    fpl_manager_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # Provenance only (which league's sync last computed this) -- NOT part of
    # the natural key, see module docstring.
    league_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("leagues.id"))
    # The most recent AVAILABLE RivalGameweekSnapshot used to derive this
    # profile's squad-dependent fields (template/differential lean, likely
    # transfer targets) -- NULL if no AVAILABLE snapshot has ever existed for
    # this rival.
    last_known_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("rival_gameweek_snapshots.id")
    )
    derived_free_transfers_min: Mapped[int | None] = mapped_column(Integer)
    derived_free_transfers_max: Mapped[int | None] = mapped_column(Integer)
    derived_free_transfers_confidence: Mapped[RivalStateConfidence | None] = mapped_column(
        SAEnum(
            RivalStateConfidence,
            name="rival_profile_derived_free_transfers_confidence",
            native_enum=False,
            validate_strings=True,
        )
    )
    derived_likely_remaining_chips_json: Mapped[list | None] = mapped_column(JSONB)
    predicted_no_transfer_probability: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    predicted_one_transfer_probability: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    predicted_multi_transfer_probability: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    predicted_hit_probability: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    predicted_captain_probabilities_json: Mapped[dict | None] = mapped_column(JSONB)
    predicted_likely_transfer_targets_json: Mapped[list | None] = mapped_column(JSONB)
    tendency_features_json: Mapped[dict | None] = mapped_column(JSONB)
    unknowns_json: Mapped[list | None] = mapped_column(JSONB)
    evidence_json: Mapped[dict | None] = mapped_column(JSONB)
    overall_confidence: Mapped[RivalStateConfidence] = mapped_column(
        SAEnum(
            RivalStateConfidence,
            name="rival_profile_overall_confidence",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
    )
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("season_id", "fpl_manager_id", name="uq_rival_profiles_season_manager"),
        Index("ix_rival_profiles_league_id", "league_id"),
    )
