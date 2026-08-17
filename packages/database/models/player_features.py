"""The ``player_features`` table: per-(player, target gameweek) engineered features for Milestone 3 Layer 2.

Populated by ``app/features/service.py``'s ``FEATURE_GENERATION`` job, sourced
entirely from data already ingested by Milestone 3 Layer 1
(``player_gameweek_stats``/``fixtures``/``gameweeks``/``players``). One row
per ``(player_id, gameweek_id, feature_version)`` — ``gameweek_id`` here is
the TARGET gameweek a set of features is meant to help predict, not a
gameweek the player necessarily played in.

Leakage-prevention guarantee
-----------------------------
Every feature column on a row is computed using only ``player_gameweek_stats``
rows whose own gameweek's ``deadline_time`` is *strictly earlier* than this
row's ``cutoff_timestamp`` (== the target gameweek's own ``deadline_time``).
``next_fixture_difficulty``/``next_fixture_was_home`` are the one deliberate
exception — fixture difficulty ratings and home/away are fixed well before a
gameweek deadline (schedule facts, not results), so referencing the target
gameweek's own fixture for those two columns is not leakage. See
``app/features/engineering.py``'s module docstring for the precise rule and
``filter_to_strictly_prior``, the single function responsible for enforcing
it.

``target_total_points`` is the label a later modeling layer will train
against: the sum of ``total_points`` across every ``player_gameweek_stats``
row for this player in the target gameweek (correctly handling a Double
Gameweek by summing rather than picking one fixture row — see migration 0008
and ``app/features/engineering.py::sum_points_for_gameweek``). It is ``NULL``
until the target gameweek is actually played (``gameweeks.finished`` is
``True``).

``price_at_time``/``ownership_at_time`` carry the exact same raw units as
``player_gameweek_stats.value_at_time``/``selected_by_count_at_time`` (and
``players.now_cost`` for the price fallback) — e.g. a price of ``75`` means
GBP7.5m in FPL's own tenths convention; ``price_at_time`` is ``Numeric(6,1)``
purely to match the DB-level formatting convention already used elsewhere in
this schema (e.g. ``players.selected_by_percent``), not because a unit
conversion happens here.

``is_missing_history=True`` means this player has zero prior
``player_gameweek_stats`` rows at all (in this season or any earlier one,
via ``fpl_code`` resolution) as of the target gameweek's deadline — this
distinguishes a genuinely brand-new player (no signal available at all) from
an established player who simply has legitimately low/zero recent form (a
real ``0`` is not the same as "unknown").
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean as SABoolean
from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base


class PlayerFeatures(Base):
    __tablename__ = "player_features"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    player_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("players.id"), nullable=False
    )
    season_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("seasons.id"), nullable=False
    )
    # The TARGET gameweek these features are meant to help predict — not
    # necessarily a gameweek this player has a player_gameweek_stats row for.
    gameweek_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("gameweeks.id"), nullable=False
    )
    feature_version: Mapped[int] = mapped_column(Integer, nullable=False)

    # == the target gameweek's own deadline_time — the leakage boundary: no
    # feature on this row may be computed from any player_gameweek_stats row
    # whose gameweek deadline is >= this timestamp (see engineering.py).
    cutoff_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # The label: sum of total_points across every fixture row this player has
    # for the target gameweek. NULL until the target gameweek is finished.
    target_total_points: Mapped[int | None] = mapped_column(Integer)

    rolling_avg_points_3gw: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    rolling_avg_points_5gw: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    rolling_avg_points_10gw: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    rolling_avg_minutes_3gw: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    rolling_avg_minutes_5gw: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    season_avg_points_to_date: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    career_avg_points_prior_seasons: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    # Same cross-season resolution as career_avg_points_prior_seasons, but
    # restricted to each prior season's own early gameweeks only (see
    # app.features.engineering.career_avg_early_season_points_prior_seasons) --
    # a player's season-opening form can differ meaningfully from their
    # full-season average (well-known slow/fast starters), which the
    # full-season column alone papers over.
    career_avg_early_season_points_prior_seasons: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    form_weighted_avg_points: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    home_avg_points: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    away_avg_points: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    # A regression-to-mean signal absent from every other column above (all points/minutes
    # history): actual (goals+assists) minus expected (xG+xA) over the most recent gameweeks that
    # carry xG data (schema-drift-null before 2022-23 -- see
    # app.features.engineering.rolling_goal_involvement_overperformance). Positive means running
    # hot relative to underlying chances, negative means due a correction.
    goal_involvement_overperformance_5gw: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    # FPL's own real 2025-26+ CBIT threshold-bonus stat (see
    # app.features.engineering.rolling_defensive_contribution) -- a genuine points-scoring signal,
    # not just an external stat; schema-drift-null before 2025-26.
    defensive_contribution_5gw: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))

    # Not leakage — fixture difficulty/home-away are schedule facts known
    # well before a deadline, not results. See module docstring.
    next_fixture_difficulty: Mapped[int | None] = mapped_column(Integer)
    next_fixture_was_home: Mapped[bool | None] = mapped_column(SABoolean)

    rest_days: Mapped[int | None] = mapped_column(Integer)
    price_at_time: Mapped[Decimal | None] = mapped_column(Numeric(6, 1))
    # Raw ownership COUNT, matching player_gameweek_stats.selected_by_count_at_time
    # (never players.selected_by_percent, a percentage).
    ownership_at_time: Mapped[int | None] = mapped_column(Integer)
    position_short_name: Mapped[str | None] = mapped_column(String(10))

    is_missing_history: Mapped[bool] = mapped_column(SABoolean, nullable=False, default=False)
    data_source: Mapped[str] = mapped_column(String(50), nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "player_id",
            "gameweek_id",
            "feature_version",
            name="uq_player_features_player_gameweek_version",
        ),
        Index("ix_player_features_season_id", "season_id"),
        Index("ix_player_features_gameweek_id", "gameweek_id"),
    )
