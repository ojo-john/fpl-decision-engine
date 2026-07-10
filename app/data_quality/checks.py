"""Pure data quality check functions.

Each function takes small in-memory row lists (dicts using raw FPL API field
names, e.g. an ``elements`` row's ``id``/``team``/``element_type``/``now_cost``)
and returns a list of ``DataQualityIssue``s — no I/O, no DB, no FPL client.

Row-level checks (severity ERROR) are quarantine-worthy: a row that fails one
of these should be persisted with ``is_quarantined=True`` rather than
excluded outright (see ``app/ingestion/normalization.py``). Run-level checks
(severity CRITICAL) indicate a systemic failure and should block the whole
ingestion run rather than quarantine any single row.
"""

from datetime import datetime, timedelta
from typing import Any

from app.data_quality.models import DataQualityIssue
from packages.domain.ingestion import DataQualitySeverity

DEFAULT_STALE_AFTER_HOURS = 26.0


def check_duplicate_fpl_ids(
    rows: list[dict[str, Any]], entity_type: str, id_key: str = "id"
) -> list[DataQualityIssue]:
    """Flag every row that shares its ``id_key`` value with at least one other row."""
    by_id: dict[Any, list[dict[str, Any]]] = {}
    for row in rows:
        by_id.setdefault(row.get(id_key), []).append(row)

    issues: list[DataQualityIssue] = []
    for fpl_id, matching_rows in by_id.items():
        if len(matching_rows) > 1:
            issues.extend(
                DataQualityIssue(
                    entity_type=entity_type,
                    entity_fpl_id=fpl_id,
                    severity=DataQualitySeverity.ERROR,
                    check_name="duplicate_fpl_id",
                    message=f"{len(matching_rows)} rows share {id_key}={fpl_id!r}",
                )
                for _ in matching_rows
            )
    return issues


def check_invalid_foreign_keys(
    rows: list[dict[str, Any]],
    entity_type: str,
    fk_specs: list[tuple[str, set[Any], str]],
    id_key: str = "id",
) -> list[DataQualityIssue]:
    """Flag rows whose foreign-key field points at an id not present in the referenced set.

    ``fk_specs`` is a list of ``(field_name, valid_ids, referenced_entity_name)``.
    A ``None`` value for a foreign-key field is treated as "no reference" and
    is never flagged here — callers with a nullable FK (e.g. fixtures'
    ``event``) should use ``check_gameweek_consistency`` instead, which
    explicitly allows ``None``.
    """
    issues: list[DataQualityIssue] = []
    for row in rows:
        fpl_id = row.get(id_key)
        for field_name, valid_ids, referenced_entity_name in fk_specs:
            value = row.get(field_name)
            if value is None:
                continue
            if value not in valid_ids:
                issues.append(
                    DataQualityIssue(
                        entity_type=entity_type,
                        entity_fpl_id=fpl_id,
                        severity=DataQualitySeverity.ERROR,
                        check_name="invalid_foreign_key",
                        message=f"{field_name}={value!r} does not reference a valid {referenced_entity_name}",
                    )
                )
    return issues


def check_impossible_prices(
    rows: list[dict[str, Any]], id_key: str = "id", cost_key: str = "now_cost"
) -> list[DataQualityIssue]:
    """Flag players with a non-positive price."""
    issues = []
    for row in rows:
        cost = row.get(cost_key)
        if cost is not None and cost <= 0:
            issues.append(
                DataQualityIssue(
                    entity_type="player",
                    entity_fpl_id=row.get(id_key),
                    severity=DataQualitySeverity.ERROR,
                    check_name="impossible_price",
                    message=f"{cost_key}={cost!r} is not a positive price",
                )
            )
    return issues


def check_negative_minutes(
    rows: list[dict[str, Any]], id_key: str = "id", minutes_key: str = "minutes"
) -> list[DataQualityIssue]:
    """Flag players with negative minutes played."""
    issues = []
    for row in rows:
        minutes = row.get(minutes_key)
        if minutes is not None and minutes < 0:
            issues.append(
                DataQualityIssue(
                    entity_type="player",
                    entity_fpl_id=row.get(id_key),
                    severity=DataQualitySeverity.ERROR,
                    check_name="negative_minutes",
                    message=f"{minutes_key}={minutes!r} is negative",
                )
            )
    return issues


def check_invalid_scores(rows: list[dict[str, Any]], id_key: str = "id") -> list[DataQualityIssue]:
    """Flag fixtures with negative scores, or finished=true with a null score."""
    issues = []
    for row in rows:
        fpl_id = row.get(id_key)
        home_score = row.get("team_h_score")
        away_score = row.get("team_a_score")
        finished = row.get("finished")

        if (home_score is not None and home_score < 0) or (away_score is not None and away_score < 0):
            issues.append(
                DataQualityIssue(
                    entity_type="fixture",
                    entity_fpl_id=fpl_id,
                    severity=DataQualitySeverity.ERROR,
                    check_name="invalid_score",
                    message=f"negative score(s): team_h_score={home_score!r}, team_a_score={away_score!r}",
                )
            )
        elif finished and (home_score is None or away_score is None):
            issues.append(
                DataQualityIssue(
                    entity_type="fixture",
                    entity_fpl_id=fpl_id,
                    severity=DataQualitySeverity.ERROR,
                    check_name="invalid_score",
                    message="fixture is finished but has a null score",
                )
            )
    return issues


def check_gameweek_consistency(
    rows: list[dict[str, Any]],
    valid_gameweek_ids: set[Any],
    id_key: str = "id",
    gameweek_key: str = "event",
) -> list[DataQualityIssue]:
    """Flag fixtures referencing a gameweek id that doesn't exist. ``event=None`` (blank/TBD) is allowed."""
    issues = []
    for row in rows:
        gameweek_ref = row.get(gameweek_key)
        if gameweek_ref is None:
            continue
        if gameweek_ref not in valid_gameweek_ids:
            issues.append(
                DataQualityIssue(
                    entity_type="fixture",
                    entity_fpl_id=row.get(id_key),
                    severity=DataQualitySeverity.ERROR,
                    check_name="invalid_gameweek_reference",
                    message=f"{gameweek_key}={gameweek_ref!r} does not reference a known gameweek",
                )
            )
    return issues


def check_at_most_one_current_gameweek(
    rows: list[dict[str, Any]], current_key: str = "is_current"
) -> list[DataQualityIssue]:
    """Run-level check: exactly one gameweek should have is_current=True."""
    count = sum(1 for row in rows if row.get(current_key))
    if count != 1:
        return [
            DataQualityIssue(
                entity_type="gameweek",
                entity_fpl_id=None,
                severity=DataQualitySeverity.CRITICAL,
                check_name="current_gameweek_count",
                message=f"expected exactly 1 gameweek with {current_key}=True, found {count}",
            )
        ]
    return []


def check_team_count(rows: list[dict[str, Any]], expected: int = 20) -> list[DataQualityIssue]:
    """Run-level check: there should be exactly ``expected`` teams (20 for the Premier League)."""
    if len(rows) != expected:
        return [
            DataQualityIssue(
                entity_type="team",
                entity_fpl_id=None,
                severity=DataQualitySeverity.CRITICAL,
                check_name="team_count",
                message=f"expected exactly {expected} teams, found {len(rows)}",
            )
        ]
    return []


def check_squad_composition_math(
    position_rows: list[dict[str, Any]], expected_squad_size: int, select_key: str = "squad_select"
) -> list[DataQualityIssue]:
    """Run-level check: sum of every position's squad_select must equal rules.squad_squadsize."""
    total = sum(row.get(select_key) or 0 for row in position_rows)
    if total != expected_squad_size:
        return [
            DataQualityIssue(
                entity_type="rules",
                entity_fpl_id=None,
                severity=DataQualitySeverity.CRITICAL,
                check_name="squad_composition_math",
                message=f"sum(positions.{select_key})={total} != rules.squad_squadsize={expected_squad_size}",
            )
        ]
    return []


def check_stale_ingestion(
    last_successful_completed_at: datetime | None,
    now: datetime,
    max_age_hours: float = DEFAULT_STALE_AFTER_HOURS,
) -> list[DataQualityIssue]:
    """Flag if the last successful ingestion run completed more than ``max_age_hours`` ago.

    No issue if there is no prior successful run to compare against.
    """
    if last_successful_completed_at is None:
        return []
    age = now - last_successful_completed_at
    if age > timedelta(hours=max_age_hours):
        return [
            DataQualityIssue(
                entity_type="endpoint",
                entity_fpl_id=None,
                severity=DataQualitySeverity.WARNING,
                check_name="stale_ingestion",
                message=f"last successful ingestion run completed {age} ago (> {max_age_hours}h)",
            )
        ]
    return []


def check_schema_drift(endpoint: str, schema_drift_detected: bool) -> list[DataQualityIssue]:
    """Flag if schema drift was detected for a given endpoint's raw_api_snapshot."""
    if schema_drift_detected:
        return [
            DataQualityIssue(
                entity_type="endpoint",
                entity_fpl_id=None,
                severity=DataQualitySeverity.WARNING,
                check_name="schema_drift",
                message=f"schema drift detected for endpoint '{endpoint}'",
            )
        ]
    return []


def check_missing_required_fields(
    rows: list[dict[str, Any]],
    entity_type: str,
    required_fields: tuple[str, ...],
    id_key: str = "id",
) -> list[DataQualityIssue]:
    """Flag rows with a blank/missing value for any of ``required_fields``."""
    issues = []
    for row in rows:
        for field_name in required_fields:
            value = row.get(field_name)
            if value is None or (isinstance(value, str) and value.strip() == ""):
                issues.append(
                    DataQualityIssue(
                        entity_type=entity_type,
                        entity_fpl_id=row.get(id_key),
                        severity=DataQualitySeverity.WARNING,
                        check_name="missing_required_field",
                        message=f"{field_name!r} is blank/missing",
                    )
                )
    return issues
