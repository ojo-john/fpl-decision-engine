"""Manager-squad/transfer domain enums for Milestone 4 Layer 1 (manager identity and squad state).

Framework-agnostic (no SQLAlchemy/FastAPI imports here), matching the style
of ``packages/domain/jobs.py``/``packages/domain/rules.py``.
"""

from enum import Enum


class ManagerSquadSnapshotSource(str, Enum):
    """How a ``manager_squad_snapshots`` row came to exist."""

    MANAGER_SYNC = "manager_sync"  # produced by a MANAGER_SYNC job against the real FPL manager API
    MANUAL_ENTRY = "manual_entry"  # produced directly via POST /squads/manual, no external I/O
