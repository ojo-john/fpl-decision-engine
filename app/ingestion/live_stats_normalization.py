"""Normalizes FPL's real ``/event/{event_id}/live/`` endpoint into ``player_gameweek_stats``.

This closes Milestone 7 Layer 0's gap: ``player_gameweek_stats`` was
previously populated ONLY by historical CSV ingestion
(``app/ingestion/historical_normalization.py``, ``data_source=
"historical_merged_gw"``) — there was no way to ingest what players actually
scored in a real, just-finished *current-season* gameweek. Rows produced
here are tagged ``data_source="live_event"``.

Real response shape (verified live against the real, public FPL API for the
real, already-finished 2025-26 season, e.g. ``GET /api/event/38/live/``)::

    {"elements": [
        {
            "id": 441,
            "stats": {
                "minutes": 61, "goals_scored": 1, "assists": 1, ...,
                "influence": "59.8", "ict_index": "13.7", ...,
                "total_points": 18, "in_dreamteam": true, "played": true
            },
            "explain": [
                {"fixture": 371, "stats": [
                    {"identifier": "minutes", "points": 2, "value": 61, "points_modification": 0},
                    {"identifier": "goals_scored", "points": 6, "value": 1, "points_modification": 0},
                    ...
                ]}
            ],
            "modified": false
        },
        ...
    ]}

Double Gameweek handling
--------------------------
A player who featured in two real fixtures in the same gameweek (a genuine,
recurring FPL mechanic — confirmed live this season, e.g. real 2025-26
event 26/33/36) gets **two** ``explain`` entries, one per fixture. The
top-level ``stats`` object is only ever a *gameweek-aggregate* across both
fixtures — it is never broken down per-fixture. ``explain[*].stats[*]``,
keyed by ``identifier``/``value``/``points``, is the *only* place the live
API attributes anything to a specific fixture, and only for the "countable"
per-appearance stats that map directly to points (see
``_EXPLAIN_RECONSTRUCTABLE_FIELDS``, verified live: ``minutes``,
``goals_scored``, ``assists``, ``clean_sheets``, ``goals_conceded``,
``own_goals``, ``penalties_saved``, ``penalties_missed``, ``yellow_cards``,
``red_cards``, ``saves``, ``bonus``, ``defensive_contribution``).

So for a genuine Double Gameweek, this module writes **two** rows (never one
doubled row — matching ``player_gameweek_stats``' own
``(player_id, gameweek_id, fixture_id)`` uniqueness, fixed for the historical
case in migration 0008), reconstructing each fixture's per-appearance stats
from ``explain`` and each fixture's ``total_points`` as the sum of that
fixture's ``explain[*].stats[*].points``. Known, unavoidable limitation:
``bps``/``ict_index``/``influence``/``creativity``/``threat``/
``expected_goals``/``expected_assists``/``expected_goal_involvements``/
``expected_goals_conceded``/``starts``/``clearances_blocks_interceptions``/
``recoveries``/``tackles`` have **no** per-fixture breakdown anywhere in this
payload — the live API only ever reports them as one gameweek-aggregate
figure. On a split row these become ``NULL`` (nullable columns — "genuinely
unknown" per this codebase's convention, never fabricated), except ``bps``
(``NOT NULL``, default ``0`` on the ``players_gameweek_stats`` schema),
which is set to ``0`` on split rows since there is no honest non-zero value
to put there.

``value_at_time``/``xp_estimate``/``selected_by_count_at_time``
------------------------------------------------------------------
``value_at_time`` is sourced from the *current* ``Player.now_cost`` — FPL
never retroactively rewrites a player's historical price, so "current price"
and "price at the time this already-finished gameweek happened" only
diverge for gameweeks that already passed by the time this ingestion runs,
which is exactly what's being backfilled here; this is a real (not
fabricated) value. ``xp_estimate`` has no live equivalent at all and is
always ``NULL``. ``selected_by_count_at_time`` (a raw ownership *count* — see
that column's own docstring, never conflated with a percentage) also has no
live equivalent: bootstrap-static only ever exposes ``selected_by_percent``
(a percentage), and deriving a count from a percentage would require the
total number of FPL managers, which this codebase doesn't ingest from
anywhere — so it is left ``NULL`` rather than fabricated from an unrelated
unit.

Quarantine convention
-----------------------
``player_id`` is ``NOT NULL`` on ``player_gameweek_stats`` (unlike
``players``/``fixtures``, there is no "quarantined but FK-null" option here)
— a row whose ``element`` doesn't resolve to a current-season ``Player`` is
dropped (never inserted) and recorded as a WARNING ``DataQualityIssue``.
This mirrors ``historical_normalization.upsert_player_gameweek_stats``'s own,
already-established convention for this exact table/column verbatim, rather
than inventing new behavior.
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data_quality import checks
from app.data_quality.models import DataQualityIssue
from app.ingestion.normalization import NormalizationCounts, _bulk_upsert, _resolve_fk
from packages.database.models.fixture import Fixture
from packages.database.models.player import Player
from packages.database.models.player_gameweek_stats import PlayerGameweekStats
from packages.database.models.position import Position
from packages.domain.ingestion import DataQualitySeverity

__all__ = [
    "LIVE_EVENT_DATA_SOURCE",
    "LiveStatsLookups",
    "resolve_live_stats_lookups",
    "parse_live_element_to_fixture_rows",
    "upsert_live_gameweek_stats",
]

LIVE_EVENT_DATA_SOURCE = "live_event"

# Per-appearance stat identifiers `explain[*].stats[*]` breaks down
# per-fixture — see the module docstring's "Double Gameweek handling"
# section. Every one of these is a NOT NULL integer column on
# PlayerGameweekStats, so a split row always gets a real (possibly zero)
# value, never NULL, for each of these.
_EXPLAIN_RECONSTRUCTABLE_FIELDS = (
    "minutes",
    "goals_scored",
    "assists",
    "clean_sheets",
    "goals_conceded",
    "own_goals",
    "penalties_saved",
    "penalties_missed",
    "yellow_cards",
    "red_cards",
    "saves",
    "bonus",
    "defensive_contribution",
)

# Decimal-typed fields the live endpoint only ever reports as one
# gameweek-aggregate figure (never broken down per-fixture in `explain`) —
# NULL on a Double Gameweek split row (all nullable columns).
_AGGREGATE_ONLY_DECIMAL_FIELDS = (
    "ict_index",
    "influence",
    "creativity",
    "threat",
    "expected_goals",
    "expected_assists",
    "expected_goal_involvements",
    "expected_goals_conceded",
)


@dataclass
class LiveStatsLookups:
    """Resolved current-season Player/Fixture lookups live-stats normalization needs.

    Built once per ingestion run (via ``resolve_live_stats_lookups``) and
    reused across every gameweek being backfilled in that run — Player/
    Fixture rows don't change mid-run.
    """

    element_to_player_id: dict[int, uuid.UUID]
    player_team_id: dict[uuid.UUID, uuid.UUID | None]
    player_position_short_name: dict[uuid.UUID, str | None]
    player_now_cost: dict[uuid.UUID, int]
    fixture_id_by_fpl_id: dict[int, uuid.UUID]
    fixture_home_away_team_id: dict[uuid.UUID, tuple[uuid.UUID | None, uuid.UUID | None]]


def resolve_live_stats_lookups(session: Session, season_id: uuid.UUID) -> LiveStatsLookups:
    """Build the current-season Player/Fixture lookups live-stats normalization needs."""
    position_short_name_by_id: dict[uuid.UUID, str] = {
        position_id: short_name
        for position_id, short_name in session.execute(
            select(Position.id, Position.short_name).where(Position.season_id == season_id)
        ).all()
    }

    element_to_player_id: dict[int, uuid.UUID] = {}
    player_team_id: dict[uuid.UUID, uuid.UUID | None] = {}
    player_position_short_name: dict[uuid.UUID, str | None] = {}
    player_now_cost: dict[uuid.UUID, int] = {}
    player_rows = session.execute(
        select(Player.id, Player.fpl_element_id, Player.team_id, Player.position_id, Player.now_cost).where(
            Player.season_id == season_id
        )
    ).all()
    for player_id, fpl_element_id, team_id, position_id, now_cost in player_rows:
        element_to_player_id[fpl_element_id] = player_id
        player_team_id[player_id] = team_id
        player_position_short_name[player_id] = (
            position_short_name_by_id.get(position_id) if position_id is not None else None
        )
        player_now_cost[player_id] = now_cost

    fixture_id_by_fpl_id: dict[int, uuid.UUID] = {}
    fixture_home_away_team_id: dict[uuid.UUID, tuple[uuid.UUID | None, uuid.UUID | None]] = {}
    fixture_rows = session.execute(
        select(Fixture.id, Fixture.fpl_fixture_id, Fixture.home_team_id, Fixture.away_team_id).where(
            Fixture.season_id == season_id
        )
    ).all()
    for fixture_id, fpl_fixture_id, home_team_id, away_team_id in fixture_rows:
        fixture_id_by_fpl_id[fpl_fixture_id] = fixture_id
        fixture_home_away_team_id[fixture_id] = (home_team_id, away_team_id)

    return LiveStatsLookups(
        element_to_player_id=element_to_player_id,
        player_team_id=player_team_id,
        player_position_short_name=player_position_short_name,
        player_now_cost=player_now_cost,
        fixture_id_by_fpl_id=fixture_id_by_fpl_id,
        fixture_home_away_team_id=fixture_home_away_team_id,
    )


def _parse_decimal_stat(value: Any) -> Decimal | None:
    """Parse a live-endpoint decimal-looking stat (e.g. ``"19.8"``). Blank/None -> ``None``."""
    if value in (None, ""):
        return None
    return Decimal(str(value))


def parse_live_element_to_fixture_rows(element: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten one live ``/live/`` ``elements[]`` entry into one row per real fixture played.

    Pure function (no I/O, no DB) — the common case (a single real fixture
    this gameweek) returns exactly one row, built directly from the
    top-level ``stats`` object. A genuine Double Gameweek (``explain`` has
    more than one entry) returns one row per fixture instead, reconstructed
    from ``explain`` — see the module docstring for exactly which columns
    can/cannot be reconstructed this way. A player with no ``explain``
    entries at all (never observed live, but defensively handled) yields a
    single row with ``fixture=None``.
    """
    explain = element.get("explain") or []
    stats = element.get("stats") or {}
    # `in_dreamteam`/`played` live inside `stats`; `modified` is a top-level
    # sibling of `stats`/`explain` -- verified against the real payload shape.
    extra = {
        "in_dreamteam": stats.get("in_dreamteam"),
        "played": stats.get("played"),
        "modified": element.get("modified"),
    }
    extra = {k: v for k, v in extra.items() if v is not None}

    if len(explain) <= 1:
        fixture_fpl_id = explain[0].get("fixture") if explain else None
        return [
            {
                "element": element["id"],
                "fixture": fixture_fpl_id,
                "minutes": stats.get("minutes", 0),
                "goals_scored": stats.get("goals_scored", 0),
                "assists": stats.get("assists", 0),
                "clean_sheets": stats.get("clean_sheets", 0),
                "goals_conceded": stats.get("goals_conceded", 0),
                "own_goals": stats.get("own_goals", 0),
                "penalties_saved": stats.get("penalties_saved", 0),
                "penalties_missed": stats.get("penalties_missed", 0),
                "yellow_cards": stats.get("yellow_cards", 0),
                "red_cards": stats.get("red_cards", 0),
                "saves": stats.get("saves", 0),
                "bonus": stats.get("bonus", 0),
                "bps": stats.get("bps", 0),
                "defensive_contribution": stats.get("defensive_contribution"),
                "clearances_blocks_interceptions": stats.get("clearances_blocks_interceptions"),
                "recoveries": stats.get("recoveries"),
                "tackles": stats.get("tackles"),
                "starts": bool(stats["starts"]) if stats.get("starts") is not None else None,
                "total_points": stats.get("total_points", 0),
                "ict_index": _parse_decimal_stat(stats.get("ict_index")),
                "influence": _parse_decimal_stat(stats.get("influence")),
                "creativity": _parse_decimal_stat(stats.get("creativity")),
                "threat": _parse_decimal_stat(stats.get("threat")),
                "expected_goals": _parse_decimal_stat(stats.get("expected_goals")),
                "expected_assists": _parse_decimal_stat(stats.get("expected_assists")),
                "expected_goal_involvements": _parse_decimal_stat(stats.get("expected_goal_involvements")),
                "expected_goals_conceded": _parse_decimal_stat(stats.get("expected_goals_conceded")),
                "extra_stats_json": dict(extra),
            }
        ]

    rows = []
    for block in explain:
        per_identifier_value = {s["identifier"]: s["value"] for s in block.get("stats", [])}
        points_sum = sum(s["points"] for s in block.get("stats", []))
        row: dict[str, Any] = {
            "element": element["id"],
            "fixture": block.get("fixture"),
            "total_points": points_sum,
            "bps": 0,  # not attributable per-fixture from /live/ — see module docstring
            "starts": None,
            "clearances_blocks_interceptions": None,
            "recoveries": None,
            "tackles": None,
            "extra_stats_json": {**extra, "double_gameweek_split": True},
        }
        for field_name in _EXPLAIN_RECONSTRUCTABLE_FIELDS:
            row[field_name] = per_identifier_value.get(field_name, 0)
        for field_name in _AGGREGATE_ONLY_DECIMAL_FIELDS:
            row[field_name] = None
        rows.append(row)
    return rows


def upsert_live_gameweek_stats(
    session: Session,
    season_id: uuid.UUID,
    gameweek_id: uuid.UUID,
    live_elements: list[dict[str, Any]],
    lookups: LiveStatsLookups,
    data_source: str = LIVE_EVENT_DATA_SOURCE,
) -> tuple[NormalizationCounts, list[DataQualityIssue]]:
    """Normalize one gameweek's live ``/live/`` elements into ``player_gameweek_stats``.

    Keyed ``(player_id, gameweek_id, fixture_id)``, identical to historical
    ingestion — a genuine Double Gameweek gets 2 rows, never one doubled row.
    See the module docstring for the full quarantine/value-sourcing
    convention.
    """
    flattened: list[dict[str, Any]] = []
    for element in live_elements:
        flattened.extend(parse_live_element_to_fixture_rows(element))

    issues: list[DataQualityIssue] = []
    issues.extend(
        checks.check_invalid_foreign_keys(
            flattened,
            entity_type="player_gameweek_stat",
            id_key="element",
            fk_specs=[
                ("element", set(lookups.element_to_player_id.keys()), "player"),
                ("fixture", set(lookups.fixture_id_by_fpl_id.keys()), "fixture"),
            ],
        )
    )

    # Defensive dedup by (element, fixture) — the live API has never been
    # observed to repeat an element, but this mirrors the exact same
    # defensive convention historical ingestion uses for the identical
    # ON CONFLICT-can't-target-the-same-row-twice constraint.
    seen_keys: set[tuple[int, int | None]] = set()
    deduped: list[dict[str, Any]] = []
    for row in flattened:
        key = (row["element"], row["fixture"])
        if key in seen_keys:
            issues.append(
                DataQualityIssue(
                    entity_type="player_gameweek_stat",
                    entity_fpl_id=row["element"],
                    severity=DataQualitySeverity.WARNING,
                    check_name="duplicate_gameweek_row",
                    message=f"element={row['element']} fixture={row['fixture']}: true duplicate row dropped",
                )
            )
            continue
        seen_keys.add(key)
        deduped.append(row)

    existing_keys = {
        (player_id, gw_id, fixture_id)
        for (player_id, gw_id, fixture_id) in session.execute(
            select(
                PlayerGameweekStats.player_id, PlayerGameweekStats.gameweek_id, PlayerGameweekStats.fixture_id
            ).where(PlayerGameweekStats.season_id == season_id)
        )
    }

    values = []
    unresolved_count = 0
    for row in deduped:
        player_id = lookups.element_to_player_id.get(row["element"])
        if player_id is None:
            unresolved_count += 1
            continue

        fixture_id = _resolve_fk(lookups.fixture_id_by_fpl_id, row["fixture"])
        team_id = lookups.player_team_id.get(player_id)
        home_team_id, away_team_id = (
            lookups.fixture_home_away_team_id.get(fixture_id, (None, None))
            if fixture_id is not None
            else (None, None)
        )
        was_home: bool | None = None
        opponent_team_id: uuid.UUID | None = None
        if team_id is not None and fixture_id is not None:
            if team_id == home_team_id:
                was_home, opponent_team_id = True, away_team_id
            elif team_id == away_team_id:
                was_home, opponent_team_id = False, home_team_id
            else:
                # Player's *current* team is neither side of this fixture —
                # most likely a mid-season transfer between this gameweek
                # and now (see module docstring's known limitations).
                # was_home/opponent_team_id are left NULL rather than
                # fabricated; every other stat on this row is unaffected.
                issues.append(
                    DataQualityIssue(
                        entity_type="player_gameweek_stat",
                        entity_fpl_id=row["element"],
                        severity=DataQualitySeverity.WARNING,
                        check_name="unresolved_opponent_team",
                        message=(
                            f"element={row['element']} fixture={row['fixture']}: player's current team "
                            "is neither side of this fixture (likely a mid-season transfer) — "
                            "was_home/opponent_team_id left NULL"
                        ),
                    )
                )

        values.append(
            {
                "id": uuid.uuid4(),
                "player_id": player_id,
                "season_id": season_id,
                "gameweek_id": gameweek_id,
                "fixture_id": fixture_id,
                "opponent_team_id": opponent_team_id,
                "was_home": was_home,
                "position_short_name_at_time": lookups.player_position_short_name.get(player_id),
                "minutes": row["minutes"],
                "total_points": row["total_points"],
                "goals_scored": row["goals_scored"],
                "assists": row["assists"],
                "clean_sheets": row["clean_sheets"],
                "goals_conceded": row["goals_conceded"],
                "own_goals": row["own_goals"],
                "penalties_saved": row["penalties_saved"],
                "penalties_missed": row["penalties_missed"],
                "yellow_cards": row["yellow_cards"],
                "red_cards": row["red_cards"],
                "saves": row["saves"],
                "bonus": row["bonus"],
                "bps": row["bps"],
                "ict_index": row["ict_index"],
                "influence": row["influence"],
                "creativity": row["creativity"],
                "threat": row["threat"],
                "expected_goals": row["expected_goals"],
                "expected_assists": row["expected_assists"],
                "expected_goal_involvements": row["expected_goal_involvements"],
                "expected_goals_conceded": row["expected_goals_conceded"],
                "starts": row["starts"],
                "defensive_contribution": row["defensive_contribution"],
                "clearances_blocks_interceptions": row["clearances_blocks_interceptions"],
                "recoveries": row["recoveries"],
                "tackles": row["tackles"],
                "xp_estimate": None,
                "value_at_time": lookups.player_now_cost.get(player_id),
                "selected_by_count_at_time": None,
                "transfers_in": None,
                "transfers_out": None,
                "transfers_balance": None,
                "data_source": data_source,
                "extra_stats_json": row["extra_stats_json"] or None,
                "is_quarantined": False,
                "quarantine_reason": None,
            }
        )

    inserted, updated = _bulk_upsert(
        session,
        PlayerGameweekStats,
        values,
        index_elements=["player_id", "gameweek_id", "fixture_id"],
        existing_keys=existing_keys,
        key_fields=["player_id", "gameweek_id", "fixture_id"],
    )

    if unresolved_count:
        issues.append(
            DataQualityIssue(
                entity_type="player_gameweek_stat",
                entity_fpl_id=None,
                severity=DataQualitySeverity.WARNING,
                check_name="unresolved_gameweek_stat_fk",
                message=f"{unresolved_count} row(s) dropped — unresolvable element reference",
            )
        )

    return NormalizationCounts(inserted=inserted, updated=updated, quarantined=0), issues
