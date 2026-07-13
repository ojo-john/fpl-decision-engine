"""Rules-driven transfer constants: free-transfer cap, sell-on fee, hit cost.

Reuses ``app.optimization.constraints``'s exact query patterns for reading
``RulesConfigField`` rows scoped to a ``rules_config_id`` — this module
doesn't reinvent that lookup logic, just applies it to a different set of
fields.

``max_extra_free_transfers``/``transfers_sell_on_fee`` are CONFIRMED fields
(sourced live from ``bootstrap-static.game_config.rules`` every rules sync)
-- a missing value here means the rules config itself is broken, so this
mirrors ``app.optimization.constraints._load_scalar_fields``'s hard-blocking
discipline for squad-legality-critical fields: raise ``RulesBlockedError``
rather than silently guessing a value that could misstate real money.

``transfer_hit_cost``/``free_transfers_per_gameweek`` are hand-maintained/
PROVISIONAL constants (see ``app/rules/constants.py``) — missing values fall
back to sane defaults with a logged warning, mirroring
``app.optimization.constraints.get_captain_multiplier``'s exact fallback
style. Never blocking on a missing provisional constant.
"""

import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.database.models.rules_config import RulesConfig
from packages.database.models.rules_config_field import RulesConfigField

logger = logging.getLogger(__name__)

DEFAULT_TRANSFER_HIT_COST = -4
DEFAULT_FREE_TRANSFERS_PER_GAMEWEEK = 1

_CONFIRMED_FIELD_PATHS = {
    "max_extra_free_transfers": "rules.max_extra_free_transfers",
    "transfers_sell_on_fee": "rules.transfers_sell_on_fee",
}

_HAND_MAINTAINED_TRANSFER_HIT_COST_PATH = "hand_maintained.transfer_hit_cost"
_HAND_MAINTAINED_FREE_TRANSFERS_PATH = "hand_maintained.free_transfers_per_gameweek"


class RulesBlockedError(Exception):
    """Raised when the confirmed rules fields needed to derive transfer constants cannot be loaded."""


@dataclass(frozen=True)
class TransferRules:
    rules_config_id: uuid.UUID
    max_extra_free_transfers: int
    free_transfers_cap: int  # 1 + max_extra_free_transfers
    transfers_sell_on_fee: Decimal
    transfer_hit_cost: int  # from hand_maintained, negative (e.g. -4)
    free_transfers_per_gameweek: int  # from hand_maintained, default 1 with graceful fallback + warning


def _load_confirmed_fields(session: Session, rules_config_id: uuid.UUID) -> dict[str, Any]:
    rows = session.execute(
        select(RulesConfigField.field_path, RulesConfigField.value_json).where(
            RulesConfigField.rules_config_id == rules_config_id,
            RulesConfigField.field_path.in_(_CONFIRMED_FIELD_PATHS.values()),
        )
    ).all()
    values_by_path: dict[str, Any] = dict(rows)  # type: ignore[arg-type]

    missing = [path for path in _CONFIRMED_FIELD_PATHS.values() if path not in values_by_path]
    if missing:
        raise RulesBlockedError(
            f"rules_config {rules_config_id} is missing required scalar field(s): {missing}"
        )

    return {key: values_by_path[path] for key, path in _CONFIRMED_FIELD_PATHS.items()}


def _load_hand_maintained_int(
    session: Session, rules_config_id: uuid.UUID, field_path: str, default: int
) -> int:
    value = session.execute(
        select(RulesConfigField.value_json).where(
            RulesConfigField.rules_config_id == rules_config_id,
            RulesConfigField.field_path == field_path,
        )
    ).scalar_one_or_none()
    if value is None:
        logger.warning(
            "%s missing for rules_config_id=%s; falling back to default %d",
            field_path,
            rules_config_id,
            default,
        )
        return default
    return int(value)


def load_transfer_rules(session: Session, rules_config_id: uuid.UUID) -> TransferRules:
    """Load the full set of transfer-related constants for a season's current rules config."""
    confirmed = _load_confirmed_fields(session, rules_config_id)
    max_extra_free_transfers = int(confirmed["max_extra_free_transfers"])
    transfers_sell_on_fee = Decimal(str(confirmed["transfers_sell_on_fee"]))

    transfer_hit_cost = _load_hand_maintained_int(
        session, rules_config_id, _HAND_MAINTAINED_TRANSFER_HIT_COST_PATH, DEFAULT_TRANSFER_HIT_COST
    )
    free_transfers_per_gameweek = _load_hand_maintained_int(
        session,
        rules_config_id,
        _HAND_MAINTAINED_FREE_TRANSFERS_PATH,
        DEFAULT_FREE_TRANSFERS_PER_GAMEWEEK,
    )

    return TransferRules(
        rules_config_id=rules_config_id,
        max_extra_free_transfers=max_extra_free_transfers,
        free_transfers_cap=1 + max_extra_free_transfers,
        transfers_sell_on_fee=transfers_sell_on_fee,
        transfer_hit_cost=transfer_hit_cost,
        free_transfers_per_gameweek=free_transfers_per_gameweek,
    )


def load_current_rules_config_id(session: Session, season_id: uuid.UUID) -> uuid.UUID:
    """Look up the current ``rules_config`` row's id for a season, raising ``RulesBlockedError`` if none."""
    rules_config_id = session.execute(
        select(RulesConfig.id).where(RulesConfig.season_id == season_id, RulesConfig.is_current.is_(True))
    ).scalar_one_or_none()
    if rules_config_id is None:
        raise RulesBlockedError(f"No current rules_config found for season {season_id}")
    return rules_config_id
