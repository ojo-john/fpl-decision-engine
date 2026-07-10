"""The ``rules_config_fields`` table: one row per flattened rule field within a rules_config version."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.domain.rules import RuleConfidence


class RulesConfigField(Base):
    __tablename__ = "rules_config_fields"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    rules_config_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("rules_config.id", ondelete="CASCADE"), nullable=False
    )
    field_path: Mapped[str] = mapped_column(String(255), nullable=False)
    value_json: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSONB)
    confidence: Mapped[RuleConfidence] = mapped_column(
        SAEnum(
            RuleConfidence, name="rules_config_field_confidence", native_enum=False, validate_strings=True
        ),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("rules_config_id", "field_path", name="uq_rules_config_fields_config_path"),
        Index("ix_rules_config_fields_field_path", "field_path"),
    )
