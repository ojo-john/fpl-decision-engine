"""Plain dataclasses for the rules-versioning system (no I/O, no SQLAlchemy)."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from packages.domain.rules import RuleConfidence


@dataclass
class RuleField:
    """One flattened, sourced rule/scoring value."""

    path: (
        str  # e.g. "rules.squad_total_spend", "scoring.goals_scored.GKP", "hand_maintained.transfer_hit_cost"
    )
    value: Any
    confidence: RuleConfidence
    source: str
    notes: str | None = None


@dataclass
class RulesConfigSnapshot:
    """A single, fully-flattened extraction of the FPL rules/scoring configuration."""

    fetched_at: datetime
    checksum_sha256: str
    fields: list[RuleField] = field(default_factory=list)
    raw_rules: dict = field(default_factory=dict)
    raw_scoring: dict = field(default_factory=dict)
    raw_chips: list = field(default_factory=list)
    raw_element_types: list = field(default_factory=list)
