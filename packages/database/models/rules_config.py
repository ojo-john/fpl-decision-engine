"""The ``rules_config`` table: one row per version of the extracted FPL rules/scoring config."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean as SABoolean
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.domain.rules import RulesStatus


class RulesConfig(Base):
    __tablename__ = "rules_config"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    season_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("seasons.id"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source_ingestion_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("ingestion_runs.id")
    )
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_rules_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    raw_scoring_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    raw_chips_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    raw_element_types_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    rules_status: Mapped[RulesStatus] = mapped_column(
        SAEnum(RulesStatus, name="rules_config_status", native_enum=False, validate_strings=True),
        nullable=False,
    )
    is_current: Mapped[bool] = mapped_column(SABoolean, nullable=False, default=False)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    change_summary_markdown: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("season_id", "version_number", name="uq_rules_config_season_version"),
        Index(
            "ix_rules_config_season_is_current",
            "season_id",
            unique=True,
            postgresql_where=text("is_current"),
        ),
    )
