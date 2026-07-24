"""Pydantic response schemas for the /models endpoints (Milestone 8)."""

import uuid
from datetime import datetime

from pydantic import BaseModel


class ChampionModelOut(BaseModel):
    """See packages.database.models.model_version.ModelVersion.is_champion."""

    model_version_id: uuid.UUID
    model_name: str
    model_family: str
    feature_version: int
    trained_at: datetime
