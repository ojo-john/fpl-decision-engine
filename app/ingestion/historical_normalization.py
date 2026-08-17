"""Normalizes vaastav/Fantasy-Premier-League historical CSV rows into the same tables live ingestion uses.

See ``app/ingestion/historical_client.py``'s module docstring for the data
source/licensing/attribution note — everything here inherits it.

Reuse strategy
---------------
CSV rows are plain strings for every field (``csv.DictReader`` never parses
types), whereas the live FPL API sends real JSON types. Rather than
reimplementing the upsert/quarantine logic that already exists in
``app/ingestion/normalization.py``, each historical row is first normalized
into a dict shaped exactly like the corresponding live API row (same field
names, real Python types), then handed to the *existing* live upsert
function wherever its behavior is already exactly right:

- ``upsert_historical_teams`` -> normalizes, then calls the live
  ``upsert_teams`` directly.
- ``upsert_historical_fixtures`` -> normalizes, then calls the live
  ``upsert_fixtures`` directly.
- ``upsert_historical_positions`` -> a thin wrapper feeding the live
  ``upsert_positions`` a hardcoded standard element-types list (see
  ``_STANDARD_ELEMENT_TYPES`` below — there is no per-season positions file
  in this historical dataset).

``upsert_historical_players`` is a dedicated variant (not a passthrough):
historical players need one extra quarantine condition live players never
do — a blank/null ``code`` (see ``check_missing_code``). ``code`` is the
durable cross-season join key (stable across a player's whole FPL career,
unlike ``fpl_element_id``, which is only stable *within* one season) — a row
missing it is still inserted (same quarantine convention as everywhere
else: never dropped outright, so FK integrity for anything referencing it
is preserved) but flagged unusable for cross-season identity resolution.

``upsert_historical_gameweeks`` and ``upsert_player_gameweek_stats`` have no
live equivalent at all (gameweeks are derived from ``merged_gw.csv`` itself
rather than a per-season events endpoint; player_gameweek_stats is an
entirely new table).

Deadline time approximation
-----------------------------
There is no ``events.json``-equivalent for historical seasons — no source
lists FPL's actual real deadline for each historical gameweek. So
``upsert_historical_gameweeks`` derives ``deadline_time`` as the earliest
``kickoff_time`` among that gameweek's rows in ``merged_gw.csv``, minus a
fixed buffer (``DEADLINE_BUFFER_MINUTES``). This is an approximation for
chronological ordering only (e.g. "this gameweek happened before that one",
"this stat line belongs to gameweek N") — it is NOT FPL's real deadline and
must never be treated as one.

Schema-era column availability
--------------------------------
``gws/merged_gw.csv``'s columns have grown over time (approximate era
boundaries — exact season each column first appeared may drift by a season,
which doesn't matter here since absent-either-way is handled identically):

- 2020-21 era: no ``expected_goals``/``expected_assists``/
  ``expected_goal_involvements``/``expected_goals_conceded``, no ``starts``,
  no ``xP``.
- ~2021-22 through ~2024-25 era: the above xG/``starts``/``xP`` columns
  present; no ``defensive_contribution``/``clearances_blocks_interceptions``/
  ``recoveries``/``tackles`` (the "defensive contribution" scoring rule
  introduced in 2025-26).
- 2025-26+ era: all of the above present.

Every stat parser here reads via ``.get()`` and treats a column that's
*entirely absent from this season's CSV header* exactly like a column
present-but-blank: both become ``None`` for optional stats (never silently
defaulted to ``0`` — a missing feature is genuinely unknown, not zero) or a
documented default for the small set of "always non-null" scoring counters
that mirror ``players``' own convention (``minutes``/``total_points``/etc).
"""

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data_quality import checks
from app.data_quality.models import DataQualityIssue
from app.ingestion.normalization import (
    NormalizationCounts,
    _bulk_upsert,
    _parse_datetime,
    _parse_decimal,
    _resolve_fk,
    upsert_fixtures,
    upsert_positions,
    upsert_teams,
)
from packages.database.models.gameweek import Gameweek
from packages.database.models.player import Player
from packages.database.models.player_gameweek_stats import PlayerGameweekStats
from packages.database.models.season import Season
from packages.domain.ingestion import DataQualitySeverity

__all__ = [
    "NormalizationCounts",
    "upsert_historical_season",
    "upsert_historical_positions",
    "upsert_historical_teams",
    "upsert_historical_players",
    "upsert_historical_gameweeks",
    "upsert_historical_fixtures",
    "upsert_player_gameweek_stats",
]

DEADLINE_BUFFER_MINUTES = 30

# FPL's four core outfield element types (GKP/DEF/MID/FWD) and their
# squad-composition rules have been stable across the entire ingestible
# historical era (2020-21 onward) — there is no per-season positions/
# element_types file in vaastav/Fantasy-Premier-League's historical dataset,
# so these are hardcoded to the exact same shape bootstrap-static's own
# `element_types` returns today (see
# tests/fixtures/fpl_api/bootstrap_static_sample.json).
#
# id=5 ("Manager"/MNG) is a 5th type confirmed present in real historical
# players_raw.csv data (e.g. 2024-25's assistant-manager feature) that is
# *not* present in the live 2025-26 bootstrap-static's `element_types`
# anymore (confirmed live) — it's included here defensively so a real
# historical row referencing it resolves its position_id correctly instead
# of being incorrectly flagged as an invalid foreign key. Squad-composition
# values are placeholders (managers were an extra, non-squad pick, not part
# of the classic 15-man squad) — never relied on for squad-legality
# validation here, only for FK resolution.
_STANDARD_ELEMENT_TYPES: list[dict[str, Any]] = [
    {
        "id": 1,
        "singular_name": "Goalkeeper",
        "singular_name_short": "GKP",
        "squad_select": 2,
        "squad_min_play": 1,
        "squad_max_play": 1,
    },
    {
        "id": 2,
        "singular_name": "Defender",
        "singular_name_short": "DEF",
        "squad_select": 5,
        "squad_min_play": 3,
        "squad_max_play": 5,
    },
    {
        "id": 3,
        "singular_name": "Midfielder",
        "singular_name_short": "MID",
        "squad_select": 5,
        "squad_min_play": 2,
        "squad_max_play": 5,
    },
    {
        "id": 4,
        "singular_name": "Forward",
        "singular_name_short": "FWD",
        "squad_select": 3,
        "squad_min_play": 1,
        "squad_max_play": 3,
    },
    {
        "id": 5,
        "singular_name": "Manager",
        "singular_name_short": "MNG",
        "squad_select": 1,
        "squad_min_play": 0,
        "squad_max_play": 1,
    },
]


# --- Small CSV-string -> typed-Python parsing helpers ------------------------
# csv.DictReader always hands back strings (or "" for a blank cell); a column
# absent from this season's header simply isn't a key in the row dict at all.
# Every helper below treats both cases (blank string, missing key) the same
# way via plain dict .get() upstream — see the module docstring.


def _is_blank(value: Any) -> bool:
    """True for ``None``, ``""``, or the literal string ``"None"``.

    Some real vaastav CSV exports (observed live on ``players_raw.csv``, e.g.
    ``chance_of_playing_next_round``/``ep_next`` for out-of-squad players)
    write a missing value as the literal 4-character string ``"None"``
    rather than leaving the cell blank — an artifact of how that column was
    written out (str(None) rather than a true empty cell). Both must be
    treated identically to a genuinely blank cell.
    """
    return value is None or value == "" or value == "None"


def _parse_bool(value: Any, default: bool = False) -> bool:
    """Parse a CSV 'True'/'False' string. Blank/None -> ``default``."""
    if _is_blank(value):
        return default
    return str(value).strip().lower() == "true"


def _parse_bool_or_none(value: Any) -> bool | None:
    """Parse a CSV 'True'/'False' string. Blank/None -> ``None`` (unknown, not False)."""
    if _is_blank(value):
        return None
    return str(value).strip().lower() == "true"


def _parse_bool_flag_or_none(value: Any) -> bool | None:
    """Parse a CSV numeric 0/1 flag column (e.g. merged_gw's ``starts``). Blank/None -> ``None``."""
    if _is_blank(value):
        return None
    return bool(int(value))


def _parse_int_or_zero(value: Any) -> int:
    if _is_blank(value):
        return 0
    return int(value)


def _parse_int_or_none(value: Any) -> int | None:
    if _is_blank(value):
        return None
    return int(value)


def _parse_decimal_or_none(value: Any) -> Decimal | None:
    if _is_blank(value):
        return None
    return Decimal(str(value))


def _blank_to_none(value: Any) -> Any:
    return None if _is_blank(value) else value


# Real, verified bug (found 2026-08-17 while building POSITION_SEGMENTATION_VALIDATION): the
# `gws/merged_gw.csv` source's own free-text "position" column is NOT the canonical FPL short
# code -- it inconsistently uses "GK" instead of "GKP" for real goalkeepers, and not even
# consistently per player: querying the real ingested data showed the SAME real goalkeeper (e.g.
# Altay Bayindir, Hakon Valdimarsson) labeled "GK" in some gameweeks and "GKP" in others, within
# the SAME season, across all 6 real historical seasons ingested. `app.modeling.candidates
# .KNOWN_POSITIONS = ("GKP", "DEF", "MID", "FWD")` only recognizes "GKP", so every "GK"-labeled
# row (the large majority of real goalkeeper rows -- e.g. 2023-24: 3406 "GK" vs only 394 "GKP")
# was silently one-hot-encoded as "unknown position" (`_one_hot_position` in candidates.py),
# not bucketed to goalkeeper at all. This affects every model ever trained on this historical
# data, including the live champion. Two other stray labels ("AM"/"MNG", 2024-25 only) were
# investigated and are NOT a bug -- they're real Assistant Manager/Manager fantasy picks (verified
# against real names: Guardiola, Arteta, Postecoglou, ...), correctly excluded from
# KNOWN_POSITIONS since they are not real player positions.
_POSITION_SHORT_NAME_ALIASES = {"GK": "GKP"}


def _normalize_position_short_name(value: Any) -> Any:
    """Canonicalize `gws/merged_gw.csv`'s own free-text position label -- see
    `_POSITION_SHORT_NAME_ALIASES`'s own comment for the real bug this closes. Unrecognized
    values (including the real, non-bug "AM"/"MNG" manager-pick labels) pass through unchanged,
    exactly as before -- this only touches the one confirmed, real alias."""
    return _POSITION_SHORT_NAME_ALIASES.get(value, value)


# --- Season -------------------------------------------------------------


def upsert_historical_season(session: Session, season_label: str) -> Season:
    """Get (or create) the ``Season`` row for a historical season.

    Never touches ``is_current`` on any row — historical seasons are always
    ``is_current=False``, and if a ``Season`` row for this label already
    exists (e.g. it's actually the still-in-progress live current season, in
    the edge case of "backfilling" the current season) its ``is_current``
    value is left completely alone. Only live ``DATA_INGESTION``
    (``get_or_create_current_season``) is ever allowed to flip that flag.
    """
    existing = session.execute(select(Season).where(Season.label == season_label)).scalar_one_or_none()
    if existing is not None:
        return existing
    season = Season(label=season_label, is_current=False)
    session.add(season)
    session.flush()
    return season


# --- Positions ------------------------------------------------------------


def upsert_historical_positions(session: Session, season_id: uuid.UUID) -> NormalizationCounts:
    """Populate the standard 4 positions for a historical season.

    Thin wrapper reusing the live ``upsert_positions`` unchanged, fed
    ``_STANDARD_ELEMENT_TYPES`` — see that constant's comment for why this is
    hardcoded rather than sourced from a per-season file.
    """
    return upsert_positions(session, season_id, _STANDARD_ELEMENT_TYPES)


# --- Teams ------------------------------------------------------------


def _normalize_team_row(row: dict[str, Any]) -> dict[str, Any]:
    """Convert one raw ``teams.csv`` row into the exact shape ``upsert_teams`` expects."""
    return {
        "id": int(row["id"]),
        "code": _parse_int_or_none(row.get("code")),
        "name": row["name"],
        "short_name": row["short_name"],
        "strength": _parse_int_or_none(row.get("strength")),
        "strength_overall_home": _parse_int_or_none(row.get("strength_overall_home")),
        "strength_overall_away": _parse_int_or_none(row.get("strength_overall_away")),
        "strength_attack_home": _parse_int_or_none(row.get("strength_attack_home")),
        "strength_attack_away": _parse_int_or_none(row.get("strength_attack_away")),
        "strength_defence_home": _parse_int_or_none(row.get("strength_defence_home")),
        "strength_defence_away": _parse_int_or_none(row.get("strength_defence_away")),
        "played": _parse_int_or_zero(row.get("played")),
        "win": _parse_int_or_zero(row.get("win")),
        "draw": _parse_int_or_zero(row.get("draw")),
        "loss": _parse_int_or_zero(row.get("loss")),
        "points": _parse_int_or_zero(row.get("points")),
        "position": _parse_int_or_none(row.get("position")),
        "unavailable": _parse_bool(row.get("unavailable"), default=False),
    }


def upsert_historical_teams(
    session: Session, season_id: uuid.UUID, raw_teams: list[dict[str, Any]], last_synced_at: datetime
) -> NormalizationCounts:
    """Normalize historical ``teams.csv`` rows and upsert via the live ``upsert_teams``.

    Keyed ``(season_id, fpl_team_id)``, identical to live teams.
    """
    normalized = [_normalize_team_row(row) for row in raw_teams]
    return upsert_teams(session, season_id, normalized, last_synced_at=last_synced_at)


# --- Players ------------------------------------------------------------


def _normalize_player_row(row: dict[str, Any]) -> dict[str, Any]:
    """Convert one raw ``players_raw.csv`` row into the exact shape ``upsert_players`` expects.

    Numeric FK/count fields become real ints (CSV gives everything as
    strings); decimal-looking fields (``selected_by_percent``/
    ``points_per_game``/``form``/``ep_this``/``ep_next``) are left as
    strings-or-``None`` — ``_parse_decimal`` (reused from the live module)
    already handles that directly, exactly like live bootstrap-static's own
    decimal-as-string fields.
    """
    return {
        "id": int(row["id"]),
        "code": _parse_int_or_none(row.get("code")),
        "team": _parse_int_or_none(row.get("team")),
        "element_type": _parse_int_or_none(row.get("element_type")),
        "first_name": _blank_to_none(row.get("first_name")),
        "second_name": _blank_to_none(row.get("second_name")),
        "web_name": _blank_to_none(row.get("web_name")),
        "now_cost": _parse_int_or_zero(row.get("now_cost")),
        "status": _blank_to_none(row.get("status")),
        "total_points": _parse_int_or_zero(row.get("total_points")),
        "minutes": _parse_int_or_zero(row.get("minutes")),
        "goals_scored": _parse_int_or_zero(row.get("goals_scored")),
        "assists": _parse_int_or_zero(row.get("assists")),
        "clean_sheets": _parse_int_or_zero(row.get("clean_sheets")),
        "goals_conceded": _parse_int_or_zero(row.get("goals_conceded")),
        "own_goals": _parse_int_or_zero(row.get("own_goals")),
        "penalties_saved": _parse_int_or_zero(row.get("penalties_saved")),
        "penalties_missed": _parse_int_or_zero(row.get("penalties_missed")),
        "yellow_cards": _parse_int_or_zero(row.get("yellow_cards")),
        "red_cards": _parse_int_or_zero(row.get("red_cards")),
        "saves": _parse_int_or_zero(row.get("saves")),
        "bonus": _parse_int_or_zero(row.get("bonus")),
        "bps": _parse_int_or_zero(row.get("bps")),
        "selected_by_percent": _blank_to_none(row.get("selected_by_percent")),
        "chance_of_playing_next_round": _parse_int_or_none(row.get("chance_of_playing_next_round")),
        "chance_of_playing_this_round": _parse_int_or_none(row.get("chance_of_playing_this_round")),
        "points_per_game": _blank_to_none(row.get("points_per_game")),
        "form": _blank_to_none(row.get("form")),
        "ep_this": _blank_to_none(row.get("ep_this")),
        "ep_next": _blank_to_none(row.get("ep_next")),
        "starts": _parse_int_or_zero(row.get("starts")),
        "news": _blank_to_none(row.get("news")),
    }


def upsert_historical_players(
    session: Session,
    season_id: uuid.UUID,
    raw_rows: list[dict[str, Any]],
    team_id_by_fpl_id: dict[int, uuid.UUID],
    position_id_by_fpl_id: dict[int, uuid.UUID],
    last_synced_at: datetime,
) -> tuple[NormalizationCounts, list[DataQualityIssue]]:
    """Historical variant of the live ``upsert_players``, plus one extra quarantine condition.

    Identical shape/quarantine-merge logic to the live version, plus
    ``check_missing_code`` — a blank/null ``code`` (this season's
    cross-season join key) is quarantine-worthy here in a way it never is
    for live ingestion (the live API always populates ``code``).

    Returns ``(NormalizationCounts, list[DataQualityIssue])`` — the caller
    persists the issues as ``data_quality_issues`` rows.
    """
    normalized = [_normalize_player_row(row) for row in raw_rows]

    issues: list[DataQualityIssue] = []
    issues.extend(checks.check_duplicate_fpl_ids(normalized, entity_type="player"))
    issues.extend(
        checks.check_invalid_foreign_keys(
            normalized,
            entity_type="player",
            fk_specs=[
                ("team", set(team_id_by_fpl_id.keys()), "team"),
                ("element_type", set(position_id_by_fpl_id.keys()), "position"),
            ],
        )
    )
    issues.extend(checks.check_impossible_prices(normalized))
    issues.extend(checks.check_negative_minutes(normalized))
    issues.extend(
        checks.check_missing_required_fields(normalized, entity_type="player", required_fields=("web_name",))
    )
    issues.extend(checks.check_missing_code(normalized))

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

    # See the matching comment in the live upsert_players: dedupe before
    # upserting so a single INSERT ... ON CONFLICT statement never targets
    # the same conflict key twice within one batch.
    seen_ids: set[int] = set()
    deduped_rows = []
    for row in normalized:
        if row["id"] in seen_ids:
            continue
        seen_ids.add(row["id"])
        deduped_rows.append(row)

    values = []
    for row in deduped_rows:
        fpl_id = row["id"]
        is_quarantined = fpl_id in quarantined_ids
        values.append(
            {
                "id": uuid.uuid4(),
                "fpl_element_id": fpl_id,
                "fpl_code": row["code"],
                "season_id": season_id,
                "team_id": _resolve_fk(team_id_by_fpl_id, row["team"]),
                "position_id": _resolve_fk(position_id_by_fpl_id, row["element_type"]),
                "first_name": row["first_name"],
                "second_name": row["second_name"],
                "web_name": row["web_name"],
                "now_cost": row["now_cost"],
                "status": row["status"],
                "total_points": row["total_points"],
                "minutes": row["minutes"],
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
                "selected_by_percent": _parse_decimal(row["selected_by_percent"]),
                "chance_of_playing_next_round": row["chance_of_playing_next_round"],
                "chance_of_playing_this_round": row["chance_of_playing_this_round"],
                "points_per_game": _parse_decimal(row["points_per_game"]),
                "form": _parse_decimal(row["form"]),
                "ep_this": _parse_decimal(row["ep_this"]),
                "ep_next": _parse_decimal(row["ep_next"]),
                "starts": row["starts"],
                "news": row["news"],
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


# --- Gameweeks (derived from merged_gw.csv, no live equivalent) -------------


def upsert_historical_gameweeks(
    session: Session, season_id: uuid.UUID, raw_merged_gw_rows: list[dict[str, Any]], last_synced_at: datetime
) -> NormalizationCounts:
    """Derive one ``Gameweek`` row per distinct ``round`` found in ``merged_gw.csv``.

    ``deadline_time`` is only a chronological-ordering approximation — see
    the module docstring's "Deadline time approximation" section. All of
    ``finished``/``data_checked`` are set ``True`` and
    ``is_previous``/``is_current``/``is_next`` all ``False`` — a historical
    season is by definition already complete, and "current"/"next" only ever
    apply to the live season.
    """
    kickoffs_by_round: dict[int, list[datetime]] = {}
    for row in raw_merged_gw_rows:
        round_num = _parse_int_or_none(row.get("round"))
        kickoff = _parse_datetime(_blank_to_none(row.get("kickoff_time")))
        if round_num is None or kickoff is None:
            continue
        kickoffs_by_round.setdefault(round_num, []).append(kickoff)

    existing_keys = {
        (season_id, fpl_id)
        for (fpl_id,) in session.execute(select(Gameweek.fpl_event_id).where(Gameweek.season_id == season_id))
    }

    values = [
        {
            "id": uuid.uuid4(),
            "fpl_event_id": round_num,
            "season_id": season_id,
            "name": f"Gameweek {round_num}",
            "deadline_time": min(kickoffs) - timedelta(minutes=DEADLINE_BUFFER_MINUTES),
            "finished": True,
            "data_checked": True,
            "is_previous": False,
            "is_current": False,
            "is_next": False,
            "average_entry_score": None,
            "highest_score": None,
            "last_synced_at": last_synced_at,
        }
        for round_num, kickoffs in sorted(kickoffs_by_round.items())
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


# --- Fixtures ------------------------------------------------------------


def _normalize_fixture_row(row: dict[str, Any]) -> dict[str, Any]:
    """Convert one raw ``fixtures.csv`` row into the exact shape ``upsert_fixtures`` expects."""
    return {
        "id": int(row["id"]),
        "event": _parse_int_or_none(row.get("event")),
        "team_h": _parse_int_or_none(row.get("team_h")),
        "team_a": _parse_int_or_none(row.get("team_a")),
        "kickoff_time": _blank_to_none(row.get("kickoff_time")),
        "finished": _parse_bool(row.get("finished")),
        "started": _parse_bool(row.get("started")),
        "team_h_score": _parse_int_or_none(row.get("team_h_score")),
        "team_a_score": _parse_int_or_none(row.get("team_a_score")),
        "team_h_difficulty": _parse_int_or_none(row.get("team_h_difficulty")),
        "team_a_difficulty": _parse_int_or_none(row.get("team_a_difficulty")),
    }


def upsert_historical_fixtures(
    session: Session,
    season_id: uuid.UUID,
    raw_fixtures: list[dict[str, Any]],
    team_id_by_fpl_id: dict[int, uuid.UUID],
    gameweek_id_by_fpl_id: dict[int, uuid.UUID],
    last_synced_at: datetime,
) -> tuple[NormalizationCounts, list[DataQualityIssue]]:
    """Normalize historical ``fixtures.csv`` rows and upsert via the live ``upsert_fixtures``.

    Keyed ``(season_id, fpl_fixture_id)``. ``gameweek_id_by_fpl_id`` here
    must be keyed by *round number* (``Gameweek.fpl_event_id`` is set to the
    round number in ``upsert_historical_gameweeks``) — the round number is
    exactly what ``fixtures.csv``'s ``event`` column references.
    """
    normalized = [_normalize_fixture_row(row) for row in raw_fixtures]
    return upsert_fixtures(
        session,
        season_id,
        normalized,
        team_id_by_fpl_id=team_id_by_fpl_id,
        gameweek_id_by_fpl_id=gameweek_id_by_fpl_id,
        last_synced_at=last_synced_at,
    )


# --- Player gameweek stats (no live equivalent) -----------------------------


def _normalize_pgw_row(row: dict[str, Any]) -> dict[str, Any]:
    """Convert one raw ``gws/merged_gw.csv`` row into typed values for ``PlayerGameweekStats``.

    ``selected`` is a raw ownership COUNT (e.g. ``"706434"``) and is read
    into ``selected_by_count_at_time`` — this is never conflated with
    ``players.selected_by_percent`` (a percentage); the two come from wholly
    different source files/columns and are stored in wholly different
    columns.
    """
    return {
        "element": _parse_int_or_none(row.get("element")),
        "round": _parse_int_or_none(row.get("round")),
        "fixture": _parse_int_or_none(row.get("fixture")),
        "opponent_team": _parse_int_or_none(row.get("opponent_team")),
        "was_home": _parse_bool_or_none(row.get("was_home")),
        "position": _normalize_position_short_name(_blank_to_none(row.get("position"))),
        "minutes": _parse_int_or_zero(row.get("minutes")),
        "total_points": _parse_int_or_zero(row.get("total_points")),
        "goals_scored": _parse_int_or_zero(row.get("goals_scored")),
        "assists": _parse_int_or_zero(row.get("assists")),
        "clean_sheets": _parse_int_or_zero(row.get("clean_sheets")),
        "goals_conceded": _parse_int_or_zero(row.get("goals_conceded")),
        "own_goals": _parse_int_or_zero(row.get("own_goals")),
        "penalties_saved": _parse_int_or_zero(row.get("penalties_saved")),
        "penalties_missed": _parse_int_or_zero(row.get("penalties_missed")),
        "yellow_cards": _parse_int_or_zero(row.get("yellow_cards")),
        "red_cards": _parse_int_or_zero(row.get("red_cards")),
        "saves": _parse_int_or_zero(row.get("saves")),
        "bonus": _parse_int_or_zero(row.get("bonus")),
        "bps": _parse_int_or_zero(row.get("bps")),
        "ict_index": _parse_decimal_or_none(row.get("ict_index")),
        "influence": _parse_decimal_or_none(row.get("influence")),
        "creativity": _parse_decimal_or_none(row.get("creativity")),
        "threat": _parse_decimal_or_none(row.get("threat")),
        "expected_goals": _parse_decimal_or_none(row.get("expected_goals")),
        "expected_assists": _parse_decimal_or_none(row.get("expected_assists")),
        "expected_goal_involvements": _parse_decimal_or_none(row.get("expected_goal_involvements")),
        "expected_goals_conceded": _parse_decimal_or_none(row.get("expected_goals_conceded")),
        "starts": _parse_bool_flag_or_none(row.get("starts")),
        "defensive_contribution": _parse_int_or_none(row.get("defensive_contribution")),
        "clearances_blocks_interceptions": _parse_int_or_none(row.get("clearances_blocks_interceptions")),
        "recoveries": _parse_int_or_none(row.get("recoveries")),
        "tackles": _parse_int_or_none(row.get("tackles")),
        "xp_estimate": _parse_decimal_or_none(row.get("xP")),
        "value_at_time": _parse_int_or_none(row.get("value")),
        "selected_by_count_at_time": _parse_int_or_none(row.get("selected")),
        "transfers_in": _parse_int_or_none(row.get("transfers_in")),
        "transfers_out": _parse_int_or_none(row.get("transfers_out")),
        "transfers_balance": _parse_int_or_none(row.get("transfers_balance")),
        "extra_stats_json": {
            key: row.get(key)
            for key in ("name", "team", "team_h_score", "team_a_score", "GW")
            if row.get(key) not in (None, "")
        },
    }


def upsert_player_gameweek_stats(
    session: Session,
    season_id: uuid.UUID,
    raw_merged_gw_rows: list[dict[str, Any]],
    element_to_player_id_map: dict[int, uuid.UUID],
    gameweek_id_by_round: dict[int, uuid.UUID],
    fixture_id_by_fpl_id: dict[int, uuid.UUID],
    team_id_by_fpl_id: dict[int, uuid.UUID],
    data_source: str = "historical_merged_gw",
) -> tuple[NormalizationCounts, list[DataQualityIssue]]:
    """Normalize ``merged_gw.csv`` rows into ``player_gameweek_stats``, keyed
    ``(player_id, gameweek_id, fixture_id)``.

    ``player_id`` is resolved via ``element`` (this season's element id,
    identical to ``players_raw.csv``'s own ``id`` column — the same id
    ``upsert_historical_players`` used as ``fpl_element_id`` when building
    ``element_to_player_id_map``) — never via ``code``, which is only needed
    for *cross*-season joins (a later milestone layer), not this
    within-season resolution.

    A row whose ``element``/``round`` doesn't resolve to a known player/
    gameweek is dropped (``player_id``/``gameweek_id`` are non-nullable
    columns — there is no "quarantined but FK-null" option here, unlike
    players/fixtures) and recorded as a WARNING-severity issue for audit.

    ``fixture_id`` is part of the uniqueness key (not just ``player_id``/
    ``gameweek_id``) specifically so a Double Gameweek — a player featuring
    twice in the same round, a real and recurring FPL mechanic, verified
    against a real historical backfill (2024-25 season, round 24) — gets a
    row per fixture instead of one silently overwriting/dropping the other.
    A row is only treated as a genuine duplicate (dropped, recorded as a
    WARNING) when the *same* ``(element, round, fixture)`` triple repeats.
    """
    normalized = [_normalize_pgw_row(row) for row in raw_merged_gw_rows]

    issues: list[DataQualityIssue] = []
    issues.extend(
        checks.check_invalid_foreign_keys(
            normalized,
            entity_type="player_gameweek_stat",
            id_key="element",
            fk_specs=[
                ("element", set(element_to_player_id_map.keys()), "player"),
                ("round", set(gameweek_id_by_round.keys()), "gameweek"),
                ("fixture", set(fixture_id_by_fpl_id.keys()), "fixture"),
                ("opponent_team", set(team_id_by_fpl_id.keys()), "team"),
            ],
        )
    )

    seen_keys: set[tuple[int, int, int | None]] = set()
    deduped: list[dict[str, Any]] = []
    for row in normalized:
        element, round_num, fixture = row["element"], row["round"], row["fixture"]
        if element is None or round_num is None:
            continue
        key = (element, round_num, fixture)
        if key in seen_keys:
            issues.append(
                DataQualityIssue(
                    entity_type="player_gameweek_stat",
                    entity_fpl_id=element,
                    severity=DataQualitySeverity.WARNING,
                    check_name="duplicate_gameweek_row",
                    message=(
                        f"element={element} round={round_num} fixture={fixture}: "
                        "true duplicate row dropped, kept first occurrence"
                    ),
                )
            )
            continue
        seen_keys.add(key)
        deduped.append(row)

    existing_keys = {
        (player_id, gameweek_id, fixture_id)
        for (player_id, gameweek_id, fixture_id) in session.execute(
            select(
                PlayerGameweekStats.player_id, PlayerGameweekStats.gameweek_id, PlayerGameweekStats.fixture_id
            ).where(PlayerGameweekStats.season_id == season_id)
        )
    }

    values = []
    unresolved_count = 0
    for row in deduped:
        player_id = element_to_player_id_map.get(row["element"])
        gameweek_id = gameweek_id_by_round.get(row["round"])
        if player_id is None or gameweek_id is None:
            unresolved_count += 1
            continue
        fixture_id = _resolve_fk(fixture_id_by_fpl_id, row["fixture"])
        values.append(
            {
                "id": uuid.uuid4(),
                "player_id": player_id,
                "season_id": season_id,
                "gameweek_id": gameweek_id,
                "fixture_id": fixture_id,
                "opponent_team_id": _resolve_fk(team_id_by_fpl_id, row["opponent_team"]),
                "was_home": row["was_home"],
                "position_short_name_at_time": row["position"],
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
                "xp_estimate": row["xp_estimate"],
                "value_at_time": row["value_at_time"],
                "selected_by_count_at_time": row["selected_by_count_at_time"],
                "transfers_in": row["transfers_in"],
                "transfers_out": row["transfers_out"],
                "transfers_balance": row["transfers_balance"],
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
                message=f"{unresolved_count} row(s) dropped — unresolvable player/gameweek reference",
            )
        )

    return NormalizationCounts(inserted=inserted, updated=updated, quarantined=0), issues
