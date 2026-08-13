"""Pydantic response schema for POST /data/refresh."""

import uuid

from pydantic import BaseModel


class DataRefreshOut(BaseModel):
    """Summary of a just-completed DATA_INGESTION run, triggered on demand from the UI.

    Deliberately a thin summary, not a full echo of the job's own
    ``output_json`` (which also carries the full ``report_markdown`` and a
    per-entity normalization breakdown -- more detail than a "refresh"
    button's caller needs; the full report is still on disk/DB via
    ``ingestion_runs`` for anyone who wants it).
    """

    ingestion_run_id: uuid.UUID
    status: str
    season_label: str | None
    data_quality_score: float | None
    optimization_permitted: bool
    players_inserted: int
    players_updated: int
    issue_count: int
