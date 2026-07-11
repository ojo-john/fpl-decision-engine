"""Rules-driven optimization constraints: squad size, budget, team limit, per-position selection.

Per-position ``squad_select``/``squad_min_play``/``squad_max_play`` come from
the already-normalized ``positions`` table (Milestone 1), not by re-parsing
``rules_config_fields`` JSON. Squad-level scalars with no relational home
(``squad_squadsize``/``squad_team_limit``/``squad_total_spend``/
``ui_currency_multiplier``) are read from ``RulesConfigField`` rows scoped to
the current ``RulesConfig`` for the season.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.database.models.position import Position
from packages.database.models.rules_config import RulesConfig
from packages.database.models.rules_config_field import RulesConfigField

logger = logging.getLogger(__name__)

DEFAULT_CAPTAIN_MULTIPLIER = 2

_SCALAR_FIELD_PATHS = {
    "squad_size": "rules.squad_squadsize",
    "team_limit": "rules.squad_team_limit",
    "total_spend_limit": "rules.squad_total_spend",
    "currency_multiplier": "rules.ui_currency_multiplier",
}


class RulesBlockedError(Exception):
    """Raised when the constraints needed to run an optimization cannot be loaded."""


@dataclass(frozen=True)
class PositionRequirement:
    position_id: uuid.UUID
    short_name: str
    squad_select: int
    squad_min_play: int
    squad_max_play: int


@dataclass(frozen=True)
class OptimizationConstraints:
    rules_config_id: uuid.UUID
    squad_size: int
    team_limit: int
    total_spend_limit: int
    currency_multiplier: int
    position_requirements: dict[str, PositionRequirement]


def _load_current_rules_config(session: Session, season_id: uuid.UUID) -> RulesConfig:
    current = session.execute(
        select(RulesConfig).where(RulesConfig.season_id == season_id, RulesConfig.is_current.is_(True))
    ).scalar_one_or_none()
    if current is None:
        raise RulesBlockedError(f"No current rules_config found for season {season_id}")
    return current


def _load_scalar_fields(session: Session, rules_config_id: uuid.UUID) -> dict[str, int]:
    rows = session.execute(
        select(RulesConfigField.field_path, RulesConfigField.value_json).where(
            RulesConfigField.rules_config_id == rules_config_id,
            RulesConfigField.field_path.in_(_SCALAR_FIELD_PATHS.values()),
        )
    ).all()
    values_by_path: dict[str, Any] = dict(rows)  # type: ignore[arg-type]

    missing = [path for path in _SCALAR_FIELD_PATHS.values() if path not in values_by_path]
    if missing:
        raise RulesBlockedError(
            f"rules_config {rules_config_id} is missing required scalar field(s): {missing}"
        )

    return {key: int(values_by_path[path]) for key, path in _SCALAR_FIELD_PATHS.items()}


def load_optimization_constraints(session: Session, season_id: uuid.UUID) -> OptimizationConstraints:
    """Load the full set of MIP constraints for a season's current rules config.

    Raises ``RulesBlockedError`` if the current rules config, its required
    scalar fields, or the season's positions cannot be found — this should
    never happen for a season whose rules status isn't already BLOCKED (see
    ``app.optimization.gate``), but is handled defensively regardless.
    """
    rules_config = _load_current_rules_config(session, season_id)
    scalars = _load_scalar_fields(session, rules_config.id)

    positions = session.execute(select(Position).where(Position.season_id == season_id)).scalars().all()
    if not positions:
        raise RulesBlockedError(f"No positions found for season {season_id}")

    position_requirements = {
        position.short_name: PositionRequirement(
            position_id=position.id,
            short_name=position.short_name,
            squad_select=position.squad_select,
            squad_min_play=position.squad_min_play,
            squad_max_play=position.squad_max_play,
        )
        for position in positions
    }

    return OptimizationConstraints(
        rules_config_id=rules_config.id,
        squad_size=scalars["squad_size"],
        team_limit=scalars["team_limit"],
        total_spend_limit=scalars["total_spend_limit"],
        currency_multiplier=scalars["currency_multiplier"],
        position_requirements=position_requirements,
    )


def get_captain_multiplier(session: Session, rules_config_id: uuid.UUID) -> int:
    """Read ``hand_maintained.captain_multiplier`` for a rules config.

    Falls back to ``DEFAULT_CAPTAIN_MULTIPLIER`` with a logged warning if the
    field is missing (e.g. deployed before any fresh rules sync has run) —
    never raises or blocks optimization over a missing provisional constant.
    """
    value = session.execute(
        select(RulesConfigField.value_json).where(
            RulesConfigField.rules_config_id == rules_config_id,
            RulesConfigField.field_path == "hand_maintained.captain_multiplier",
        )
    ).scalar_one_or_none()
    if value is None:
        logger.warning(
            "hand_maintained.captain_multiplier missing for rules_config_id=%s; "
            "falling back to default multiplier %d",
            rules_config_id,
            DEFAULT_CAPTAIN_MULTIPLIER,
        )
        return DEFAULT_CAPTAIN_MULTIPLIER
    return int(value)
