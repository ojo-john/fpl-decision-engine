"""The ``ingestion_runs`` table: one row per rules_sync/data_ingestion job run."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean as SABoolean
from sqlalchemy import DateTime, ForeignKey, Numeric, Text, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.domain.ingestion import EndpointHealth, IngestionRunStatus, IngestionRunType


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("jobs.id"))
    run_type: Mapped[IngestionRunType] = mapped_column(
        SAEnum(IngestionRunType, name="ingestion_run_type", native_enum=False, validate_strings=True),
        nullable=False,
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[IngestionRunStatus] = mapped_column(
        SAEnum(IngestionRunStatus, name="ingestion_run_status", native_enum=False, validate_strings=True),
        nullable=False,
        default=IngestionRunStatus.RUNNING,
    )
    overall_health: Mapped[EndpointHealth | None] = mapped_column(
        SAEnum(EndpointHealth, name="ingestion_run_overall_health", native_enum=False, validate_strings=True)
    )
    data_quality_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    optimization_permitted: Mapped[bool | None] = mapped_column(SABoolean)
    report_markdown: Mapped[str | None] = mapped_column(Text)
    summary_json: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
