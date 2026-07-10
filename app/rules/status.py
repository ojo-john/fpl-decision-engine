"""Computes RulesStatus: whether the current rules config is trustworthy enough to optimize against."""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.rules.models import RuleField, RulesConfigSnapshot
from packages.database.models.rules_config import RulesConfig
from packages.domain.ingestion import EndpointHealth
from packages.domain.rules import RulesStatus

STALE_AFTER = timedelta(hours=48)

_CORE_REQUIRED_PATHS = (
    "rules.squad_squadsize",
    "rules.squad_squadplay",
    "rules.squad_team_limit",
    "rules.squad_total_spend",
    "scoring.assists",
    "scoring.bonus",
)


def _fields_by_path(snapshot: RulesConfigSnapshot) -> dict[str, RuleField]:
    return {f.path: f for f in snapshot.fields}


def _element_type_squad_select_paths(fields_by_path: dict[str, RuleField]) -> list[str]:
    return [p for p in fields_by_path if p.startswith("element_types.") and p.endswith(".squad_select")]


def _missing_squad_legality_critical_fields(fields_by_path: dict[str, RuleField]) -> bool:
    if any(path not in fields_by_path for path in _CORE_REQUIRED_PATHS):
        return True

    select_paths = _element_type_squad_select_paths(fields_by_path)
    if not select_paths:
        return True

    position_short_names = {p.split(".")[1] for p in select_paths}
    for short_name in position_short_names:
        if f"scoring.goals_scored.{short_name}" not in fields_by_path:
            return True
        if f"scoring.clean_sheets.{short_name}" not in fields_by_path:
            return True
    return False


def _squad_select_sum_mismatches_squadsize(fields_by_path: dict[str, RuleField]) -> bool:
    squad_size_field = fields_by_path.get("rules.squad_squadsize")
    if squad_size_field is None:
        return True
    select_paths = _element_type_squad_select_paths(fields_by_path)
    total = sum(fields_by_path[p].value for p in select_paths)
    return total != squad_size_field.value


def compute_rules_status(
    snapshot: RulesConfigSnapshot,
    endpoint_health: EndpointHealth,
    last_confirmed_at: datetime | None,
) -> RulesStatus:
    """Compute the overall trustworthiness of a rules snapshot.

    BLOCKED: the bootstrap fetch failed with no prior confirmed config to
    fall back on, or a squad-legality-critical field is missing/inconsistent
    (never optimize against an incomplete/self-contradictory ruleset).

    DEGRADED: the fetch succeeded but was flaky (retries/high latency), or
    the last confirmed config is more than 48 hours stale. This never blocks
    optimization by itself — it's a caution signal only.

    OK: otherwise.
    """
    fields_by_path = _fields_by_path(snapshot)

    if endpoint_health == EndpointHealth.BLOCKED and last_confirmed_at is None:
        return RulesStatus.BLOCKED
    if _missing_squad_legality_critical_fields(fields_by_path):
        return RulesStatus.BLOCKED
    if _squad_select_sum_mismatches_squadsize(fields_by_path):
        return RulesStatus.BLOCKED

    if endpoint_health == EndpointHealth.DEGRADED:
        return RulesStatus.DEGRADED
    if last_confirmed_at is not None and (snapshot.fetched_at - last_confirmed_at) > STALE_AFTER:
        return RulesStatus.DEGRADED

    return RulesStatus.OK


def get_current_rules_status(session: Session, season_id: uuid.UUID) -> RulesStatus:
    """DB-backed wrapper: look up the current rules_config row's status for a season.

    Returns BLOCKED if no rules_config has ever been synced for this season.
    """
    current = session.execute(
        select(RulesConfig).where(RulesConfig.season_id == season_id, RulesConfig.is_current.is_(True))
    ).scalar_one_or_none()
    if current is None:
        return RulesStatus.BLOCKED
    return RulesStatus(current.rules_status)
