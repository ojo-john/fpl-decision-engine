"""Normalizes raw FPL API rows into the seasons/positions/teams/players/gameweeks/fixtures tables.

Quarantine convention
----------------------
Before a row is upserted, the relevant ``app/data_quality/checks.py``
functions run against the staged (raw) rows. Any row that fails a row-level
(``ERROR`` severity) check is still upserted — never dropped outright, so
foreign-key integrity for other entities that reference it is preserved —
but with ``is_quarantined=True`` and ``quarantine_reason=<check_name>:
<message>`` set.

**Every future reader must respect this convention**: always filter active
data with ``WHERE NOT is_quarantined`` (or the ORM equivalent,
``Model.is_quarantined.is_(False)``). A quarantined row's other columns may
be incomplete or unreliable (e.g. an unresolved foreign key becomes
``NULL``) — it is kept only for referential integrity and audit purposes,
not for use in modeling/optimization.

Only ``players`` and ``fixtures`` get ``is_quarantined``; bad ``teams`` or
``gameweeks`` rows indicate a systemic/critical failure (wrong team count,
two "current" gameweeks) that should block the whole run via the data
quality gate (see ``app/data_quality/checks.py``), not be quarantined
per-row.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.data_quality import checks
from packages.database.models.fixture import Fixture
from packages.database.models.gameweek import Gameweek
from packages.database.models.player import Player
from packages.database.models.position import Position
from packages.database.models.team import Team


@dataclass
class NormalizationCounts:
    inserted: int
    updated: int
    quarantined: int


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _parse_decimal(value) -> Decimal | None:
    """Defensively parse a numeric field FPL may send as a string, ``null``, or ``""``.

    Applied to ``points_per_game``/``form``/``ep_this``/``ep_next``, which
    bootstrap-static sends as decimal-looking strings (e.g. ``"4.4"``).
    """
    if value in (None, ""):
        return None
    return Decimal(str(value))


def _resolve_fk(id_map: dict[int, uuid.UUID], fpl_id: int | None) -> uuid.UUID | None:
    """Resolve a raw FPL id to its internal UUID, or None if unresolvable/absent.

    A plain ``id_map.get(fpl_id)`` doesn't type-check when ``fpl_id`` is
    ``int | None`` and ``id_map`` is keyed by ``int`` — this makes the
    "missing/unresolvable foreign key becomes NULL" behavior explicit.
    """
    if fpl_id is None:
        return None
    return id_map.get(fpl_id)


# Postgres' extended-query protocol caps a single statement at 65535 bound
# parameters. Live endpoints (~800 players/~380 fixtures/season) never came
# close, but a full historical season's player_gameweek_stats can be
# ~25k-28k rows * ~45 columns — tens of millions of parameters in one
# INSERT — so every batch must be chunked to stay safely under that limit.
_MAX_BIND_PARAMS = 65535


def _bulk_upsert(
    session: Session,
    model: type,
    values: list[dict],
    index_elements: list[str],
    existing_keys: set[tuple],
    key_fields: list[str],
) -> tuple[int, int]:
    """Upsert ``values`` into ``model``'s table, returning (inserted_count, updated_count).

    ``existing_keys`` is the set of ``key_fields``-tuples already present in
    the table *before* this call, used purely to classify each row as an
    insert or an update (Postgres' ``INSERT ... ON CONFLICT`` doesn't itself
    report which rows were new). Automatically chunks ``values`` into
    multiple ``INSERT`` statements if a single one would exceed Postgres'
    bound-parameter limit (see ``_MAX_BIND_PARAMS``) — transparent to every
    caller, live or historical.
    """
    if not values:
        return (0, 0)

    inserted = 0
    updated = 0
    for row in values:
        key = tuple(row[field] for field in key_fields)
        if key in existing_keys:
            updated += 1
        else:
            inserted += 1

    columns_per_row = len(values[0])
    chunk_size = max(1, _MAX_BIND_PARAMS // columns_per_row)

    for start in range(0, len(values), chunk_size):
        chunk = values[start : start + chunk_size]
        stmt = insert(model).values(chunk)
        update_columns = {
            col.name: col
            for col in stmt.excluded
            if col.name not in index_elements and col.name not in ("id", "created_at")
        }
        stmt = stmt.on_conflict_do_update(index_elements=index_elements, set_=update_columns)
        session.execute(stmt)

    return (inserted, updated)


def upsert_positions(session: Session, season_id: uuid.UUID, raw_rows: list[dict]) -> NormalizationCounts:
    """Normalize FPL ``element_types`` rows into the ``positions`` table."""
    existing_keys = {
        (season_id, fpl_id)
        for (fpl_id,) in session.execute(
            select(Position.fpl_element_type_id).where(Position.season_id == season_id)
        )
    }

    values = [
        {
            "id": uuid.uuid4(),
            "fpl_element_type_id": row["id"],
            "season_id": season_id,
            "singular_name": row["singular_name"],
            "short_name": row["singular_name_short"],
            "squad_select": row["squad_select"],
            "squad_min_play": row["squad_min_play"],
            "squad_max_play": row["squad_max_play"],
        }
        for row in raw_rows
    ]

    inserted, updated = _bulk_upsert(
        session,
        Position,
        values,
        index_elements=["season_id", "fpl_element_type_id"],
        existing_keys=existing_keys,
        key_fields=["season_id", "fpl_element_type_id"],
    )
    return NormalizationCounts(inserted=inserted, updated=updated, quarantined=0)


def upsert_teams(
    session: Session, season_id: uuid.UUID, raw_rows: list[dict], last_synced_at: datetime
) -> NormalizationCounts:
    """Normalize FPL ``teams`` rows into the ``teams`` table."""
    existing_keys = {
        (season_id, fpl_id)
        for (fpl_id,) in session.execute(select(Team.fpl_team_id).where(Team.season_id == season_id))
    }

    values = [
        {
            "id": uuid.uuid4(),
            "fpl_team_id": row["id"],
            "season_id": season_id,
            "code": row.get("code"),
            "name": row["name"],
            "short_name": row["short_name"],
            "strength": row.get("strength"),
            "strength_overall_home": row.get("strength_overall_home"),
            "strength_overall_away": row.get("strength_overall_away"),
            "strength_attack_home": row.get("strength_attack_home"),
            "strength_attack_away": row.get("strength_attack_away"),
            "strength_defence_home": row.get("strength_defence_home"),
            "strength_defence_away": row.get("strength_defence_away"),
            "played": row.get("played", 0),
            "win": row.get("win", 0),
            "draw": row.get("draw", 0),
            "loss": row.get("loss", 0),
            "points": row.get("points", 0),
            "position": row.get("position"),
            "unavailable": row.get("unavailable", False),
            "last_synced_at": last_synced_at,
        }
        for row in raw_rows
    ]

    inserted, updated = _bulk_upsert(
        session,
        Team,
        values,
        index_elements=["season_id", "fpl_team_id"],
        existing_keys=existing_keys,
        key_fields=["season_id", "fpl_team_id"],
    )
    return NormalizationCounts(inserted=inserted, updated=updated, quarantined=0)


def upsert_players(
    session: Session,
    season_id: uuid.UUID,
    raw_rows: list[dict],
    team_id_by_fpl_id: dict[int, uuid.UUID],
    position_id_by_fpl_id: dict[int, uuid.UUID],
    last_synced_at: datetime,
) -> tuple[NormalizationCounts, list]:
    """Normalize FPL ``elements`` rows into the ``players`` table.

    Returns ``(NormalizationCounts, list[DataQualityIssue])`` — the caller is
    responsible for persisting the issues as ``data_quality_issues`` rows.
    """
    issues = []
    issues.extend(checks.check_duplicate_fpl_ids(raw_rows, entity_type="player"))
    issues.extend(
        checks.check_invalid_foreign_keys(
            raw_rows,
            entity_type="player",
            fk_specs=[
                ("team", set(team_id_by_fpl_id.keys()), "team"),
                ("element_type", set(position_id_by_fpl_id.keys()), "position"),
            ],
        )
    )
    issues.extend(checks.check_impossible_prices(raw_rows))
    issues.extend(checks.check_negative_minutes(raw_rows))
    issues.extend(
        checks.check_missing_required_fields(raw_rows, entity_type="player", required_fields=("web_name",))
    )

    quarantined_ids: dict[int, str] = {}
    for issue in issues:
        if issue.severity.value == "error" and issue.entity_fpl_id is not None:
            reason = f"{issue.check_name}: {issue.message}"
            if issue.entity_fpl_id in quarantined_ids:
                quarantined_ids[issue.entity_fpl_id] += f"; {reason}"
            else:
                quarantined_ids[issue.entity_fpl_id] = reason

    existing_keys = {
        (season_id, fpl_id)
        for (fpl_id,) in session.execute(select(Player.fpl_element_id).where(Player.season_id == season_id))
    }

    # A single INSERT ... ON CONFLICT statement cannot affect the same
    # conflict target twice (Postgres raises "ON CONFLICT DO UPDATE command
    # cannot affect row a second time"), so duplicate fpl_element_ids within
    # this batch must be deduplicated before upserting — the first
    # occurrence wins. The duplicate_fpl_id issue above is still recorded
    # for every original occurrence, so the collision is never silently lost.
    seen_ids: set[int] = set()
    deduped_rows = []
    for row in raw_rows:
        if row["id"] in seen_ids:
            continue
        seen_ids.add(row["id"])
        deduped_rows.append(row)

    values = []
    for row in deduped_rows:
        fpl_id = row["id"]
        is_quarantined = fpl_id in quarantined_ids
        selected_by_percent = row.get("selected_by_percent")
        values.append(
            {
                "id": uuid.uuid4(),
                "fpl_element_id": fpl_id,
                "fpl_code": row.get("code"),
                "season_id": season_id,
                "team_id": _resolve_fk(team_id_by_fpl_id, row.get("team")),
                "position_id": _resolve_fk(position_id_by_fpl_id, row.get("element_type")),
                "first_name": row.get("first_name"),
                "second_name": row.get("second_name"),
                "web_name": row.get("web_name"),
                "now_cost": row["now_cost"],
                "status": row.get("status"),
                "total_points": row.get("total_points", 0),
                "minutes": row.get("minutes", 0),
                "goals_scored": row.get("goals_scored", 0),
                "assists": row.get("assists", 0),
                "clean_sheets": row.get("clean_sheets", 0),
                "goals_conceded": row.get("goals_conceded", 0),
                "own_goals": row.get("own_goals", 0),
                "penalties_saved": row.get("penalties_saved", 0),
                "penalties_missed": row.get("penalties_missed", 0),
                "yellow_cards": row.get("yellow_cards", 0),
                "red_cards": row.get("red_cards", 0),
                "saves": row.get("saves", 0),
                "bonus": row.get("bonus", 0),
                "bps": row.get("bps", 0),
                "selected_by_percent": selected_by_percent,
                "chance_of_playing_next_round": row.get("chance_of_playing_next_round"),
                "chance_of_playing_this_round": row.get("chance_of_playing_this_round"),
                "points_per_game": _parse_decimal(row.get("points_per_game")),
                "form": _parse_decimal(row.get("form")),
                "ep_this": _parse_decimal(row.get("ep_this")),
                "ep_next": _parse_decimal(row.get("ep_next")),
                "starts": row.get("starts", 0),
                "news": row.get("news"),
                "is_quarantined": is_quarantined,
                "quarantine_reason": quarantined_ids.get(fpl_id),
                "last_synced_at": last_synced_at,
            }
        )

    inserted, updated = _bulk_upsert(
        session,
        Player,
        values,
        index_elements=["season_id", "fpl_element_id"],
        existing_keys=existing_keys,
        key_fields=["season_id", "fpl_element_id"],
    )
    return NormalizationCounts(inserted=inserted, updated=updated, quarantined=len(quarantined_ids)), issues


def upsert_gameweeks(
    session: Session, season_id: uuid.UUID, raw_rows: list[dict], last_synced_at: datetime
) -> NormalizationCounts:
    """Normalize FPL ``events`` rows into the ``gameweeks`` table."""
    existing_keys = {
        (season_id, fpl_id)
        for (fpl_id,) in session.execute(select(Gameweek.fpl_event_id).where(Gameweek.season_id == season_id))
    }

    values = [
        {
            "id": uuid.uuid4(),
            "fpl_event_id": row["id"],
            "season_id": season_id,
            "name": row["name"],
            "deadline_time": _parse_datetime(row["deadline_time"]),
            "finished": row.get("finished", False),
            "data_checked": row.get("data_checked", False),
            "is_previous": row.get("is_previous", False),
            "is_current": row.get("is_current", False),
            "is_next": row.get("is_next", False),
            "average_entry_score": row.get("average_entry_score"),
            "highest_score": row.get("highest_score"),
            "last_synced_at": last_synced_at,
        }
        for row in raw_rows
    ]

    inserted, updated = _bulk_upsert(
        session,
        Gameweek,
        values,
        index_elements=["season_id", "fpl_event_id"],
        existing_keys=existing_keys,
        key_fields=["season_id", "fpl_event_id"],
    )
    return NormalizationCounts(inserted=inserted, updated=updated, quarantined=0)


def upsert_fixtures(
    session: Session,
    season_id: uuid.UUID,
    raw_rows: list[dict],
    team_id_by_fpl_id: dict[int, uuid.UUID],
    gameweek_id_by_fpl_id: dict[int, uuid.UUID],
    last_synced_at: datetime,
) -> tuple[NormalizationCounts, list]:
    """Normalize FPL ``fixtures`` rows into the ``fixtures`` table.

    Returns ``(NormalizationCounts, list[DataQualityIssue])``.
    """
    issues = []
    issues.extend(checks.check_duplicate_fpl_ids(raw_rows, entity_type="fixture"))
    issues.extend(
        checks.check_invalid_foreign_keys(
            raw_rows,
            entity_type="fixture",
            fk_specs=[
                ("team_h", set(team_id_by_fpl_id.keys()), "team"),
                ("team_a", set(team_id_by_fpl_id.keys()), "team"),
            ],
        )
    )
    issues.extend(checks.check_invalid_scores(raw_rows))
    issues.extend(
        checks.check_gameweek_consistency(raw_rows, valid_gameweek_ids=set(gameweek_id_by_fpl_id.keys()))
    )

    quarantined_ids: dict[int, str] = {}
    for issue in issues:
        if issue.severity.value == "error" and issue.entity_fpl_id is not None:
            reason = f"{issue.check_name}: {issue.message}"
            if issue.entity_fpl_id in quarantined_ids:
                quarantined_ids[issue.entity_fpl_id] += f"; {reason}"
            else:
                quarantined_ids[issue.entity_fpl_id] = reason

    existing_keys = {
        (season_id, fpl_id)
        for (fpl_id,) in session.execute(select(Fixture.fpl_fixture_id).where(Fixture.season_id == season_id))
    }

    # See the matching comment in upsert_players: dedupe before upserting so
    # a single INSERT ... ON CONFLICT statement never targets the same
    # conflict key twice within one batch.
    seen_ids: set[int] = set()
    deduped_rows = []
    for row in raw_rows:
        if row["id"] in seen_ids:
            continue
        seen_ids.add(row["id"])
        deduped_rows.append(row)

    values = []
    for row in deduped_rows:
        fpl_id = row["id"]
        is_quarantined = fpl_id in quarantined_ids
        event = row.get("event")
        values.append(
            {
                "id": uuid.uuid4(),
                "fpl_fixture_id": fpl_id,
                "season_id": season_id,
                "gameweek_id": _resolve_fk(gameweek_id_by_fpl_id, event),
                "home_team_id": _resolve_fk(team_id_by_fpl_id, row.get("team_h")),
                "away_team_id": _resolve_fk(team_id_by_fpl_id, row.get("team_a")),
                "kickoff_time": _parse_datetime(row.get("kickoff_time")),
                "finished": row.get("finished", False),
                "started": row.get("started", False),
                "home_score": row.get("team_h_score"),
                "away_score": row.get("team_a_score"),
                "home_difficulty": row.get("team_h_difficulty"),
                "away_difficulty": row.get("team_a_difficulty"),
                "is_quarantined": is_quarantined,
                "quarantine_reason": quarantined_ids.get(fpl_id),
                "last_synced_at": last_synced_at,
            }
        )

    inserted, updated = _bulk_upsert(
        session,
        Fixture,
        values,
        index_elements=["season_id", "fpl_fixture_id"],
        existing_keys=existing_keys,
        key_fields=["season_id", "fpl_fixture_id"],
    )
    return NormalizationCounts(inserted=inserted, updated=updated, quarantined=len(quarantined_ids)), issues
