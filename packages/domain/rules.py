"""Rules-versioning domain enums.

Framework-agnostic (no SQLAlchemy/FastAPI imports here), matching the style
of ``packages/domain/jobs.py``.
"""

from enum import Enum


class RuleConfidence(str, Enum):
    """How authoritative a given rule field's value is."""

    CONFIRMED = "confirmed"  # sourced live from bootstrap-static this run
    PROVISIONAL = "provisional"  # hand-maintained, not present in the API, unconfirmed
    DEPRECATED = "deprecated"  # existed in a prior version, absent/unused in current snapshot


class RulesStatus(str, Enum):
    """Overall health of the current rules configuration."""

    OK = "ok"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
