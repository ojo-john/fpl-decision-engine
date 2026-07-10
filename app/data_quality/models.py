"""Plain dataclass representing a single data quality issue (no I/O, no SQLAlchemy)."""

from dataclasses import dataclass

from packages.domain.ingestion import DataQualitySeverity


@dataclass
class DataQualityIssue:
    entity_type: str  # 'player' | 'team' | 'fixture' | 'gameweek' | 'rules' | 'endpoint'
    entity_fpl_id: int | None
    severity: DataQualitySeverity
    check_name: str
    message: str
