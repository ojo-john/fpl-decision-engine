"""The ``raw_api_snapshots`` table: verbatim record of every upstream FPL API fetch."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean as SABoolean
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.domain.ingestion import EndpointHealth


class RawApiSnapshot(Base):
    __tablename__ = "raw_api_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ingestion_run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("ingestion_runs.id", ondelete="CASCADE"), nullable=False
    )
    endpoint: Mapped[str] = mapped_column(String(100), nullable=False)
    request_url: Mapped[str] = mapped_column(String(500), nullable=False)
    request_params_json: Mapped[dict | None] = mapped_column(JSONB)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    health_status: Mapped[EndpointHealth] = mapped_column(
        SAEnum(
            EndpointHealth, name="raw_api_snapshot_health_status", native_enum=False, validate_strings=True
        ),
        nullable=False,
    )
    checksum_sha256: Mapped[str | None] = mapped_column(String(64))
    schema_signature: Mapped[str | None] = mapped_column(String(64))
    schema_drift_detected: Mapped[bool] = mapped_column(SABoolean, nullable=False, default=False)
    raw_body: Mapped[dict | list | None] = mapped_column(JSONB)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_raw_api_snapshots_endpoint_retrieved_at", "endpoint", "retrieved_at"),)
