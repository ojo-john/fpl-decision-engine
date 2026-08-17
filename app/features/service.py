"""Orchestrates Milestone 3 Layer 2's feature generation: DB in, pure functions in ``engineering.py``, DB out.

Two entry points:

- ``compute_features_for_player_gameweek`` — computes and returns a single
  (unsaved) ``PlayerFeatures`` row for one player/target-gameweek pair. Handy
  for ad-hoc use and focused tests; issues its own small, targeted queries.
- ``generate_features_for_season`` — computes and upserts ``PlayerFeatures``
  rows for every (non-quarantined) player x every gameweek in a season.
  Preloads this season's ``player_gameweek_stats``/``fixtures`` and every
  cross-season row reachable via ``fpl_code`` once, up front, rather than
  issuing thousands of tiny per-player-per-gameweek queries.

Both funnel their assembled, plain-value inputs through
``_build_player_features_values`` so the two entry points can never silently
diverge in what they compute.
"""

import uuid
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.features import engineering
from app.features.engineering import PriorGameweekStat
from app.ingestion.normalization import NormalizationCounts, _bulk_upsert
from packages.database.models.fixture import Fixture
from packages.database.models.gameweek import Gameweek
from packages.database.models.player import Player
from packages.database.models.player_features import PlayerFeatures
from packages.database.models.player_gameweek_stats import PlayerGameweekStats
from packages.database.models.position import Position
from packages.database.models.season import Season
from packages.database.models.team import Team

__all__ = [
    "FEATURE_VERSION",
    "compute_features_for_player_gameweek",
    "generate_features_for_season",
]

# Bump whenever the computation logic in engineering.py (or the resolution
# logic in this module) changes in a way that would produce different values
# for the same player/gameweek — old rows are never overwritten in place by a
# new version, they coexist (see the (player_id, gameweek_id, feature_version)
# uniqueness constraint), so a model trained against an old version can still
# be reproduced.
FEATURE_VERSION = 1

# Half-life for form_weighted_avg_points: a gameweek 3 rounds ago is weighted
# half as much as the most recent one. Chosen as a reasonable "recent form"
# window for FPL (roughly a month of fixtures) — revisit alongside
# FEATURE_VERSION if Layer 3 modeling finds a different half-life predicts
# better.
FORM_HALF_LIFE_GAMEWEEKS = 3.0

FEATURE_DATA_SOURCE = "feature_engineering_v1"


def _resolve_elo_differential(
    session: Session, team_id: uuid.UUID | None, opponent_team_id: uuid.UUID | None
) -> Decimal | None:
    """This team's ClubElo rating minus the opponent's, or ``None`` if either team is unknown or
    has no rating yet (``TEAM_STRENGTH_SYNC`` hasn't run, or one side couldn't be name-matched --
    see ``app.ingestion.team_strength``). Two point lookups, not preloaded -- only used by the
    single-player entry point; ``generate_features_for_season``'s bulk path preloads all of a
    season's ratings once instead (see its own ``elo_by_team_id`` dict)."""
    if team_id is None or opponent_team_id is None:
        return None
    team = session.get(Team, team_id)
    opponent = session.get(Team, opponent_team_id)
    if team is None or opponent is None or team.elo_rating is None or opponent.elo_rating is None:
        return None
    return team.elo_rating - opponent.elo_rating


def _build_player_features_values(
    *,
    player_id: uuid.UUID,
    season_id: uuid.UUID,
    gameweek_id: uuid.UUID,
    feature_version: int,
    target_deadline: datetime,
    target_finished: bool,
    current_prior: list[PriorGameweekStat],
    other_prior: list[PriorGameweekStat],
    target_gw_points: list[int],
    next_fixture_difficulty: int | None,
    next_fixture_was_home: bool | None,
    next_fixture_elo_differential: Decimal | None,
    rest_days_value: int | None,
    price_at_time: int | None,
    ownership_at_time: int | None,
    position_short_name: str | None,
    computed_at: datetime,
) -> dict[str, Any]:
    """Assemble one player_features row's column values from already-resolved plain inputs.

    The one function both ``compute_features_for_player_gameweek`` and
    ``generate_features_for_season`` funnel through, so the two entry points
    can never compute a different answer for the same inputs.
    """
    is_missing_history = (len(current_prior) + len(other_prior)) == 0
    home_avg_points, away_avg_points = engineering.home_away_avg_points(current_prior)
    target_total_points = engineering.sum_points_for_gameweek(target_gw_points) if target_finished else None

    return {
        "id": uuid.uuid4(),
        "player_id": player_id,
        "season_id": season_id,
        "gameweek_id": gameweek_id,
        "feature_version": feature_version,
        "cutoff_timestamp": target_deadline,
        "computed_at": computed_at,
        "target_total_points": target_total_points,
        "rolling_avg_points_3gw": engineering.rolling_avg_points(current_prior, 3),
        "rolling_avg_points_5gw": engineering.rolling_avg_points(current_prior, 5),
        "rolling_avg_points_10gw": engineering.rolling_avg_points(current_prior, 10),
        "rolling_avg_minutes_3gw": engineering.rolling_avg_minutes(current_prior, 3),
        "rolling_avg_minutes_5gw": engineering.rolling_avg_minutes(current_prior, 5),
        "season_avg_points_to_date": engineering.season_avg_points_to_date(current_prior),
        "career_avg_points_prior_seasons": engineering.career_avg_points_prior_seasons(other_prior),
        "career_avg_early_season_points_prior_seasons": (
            engineering.career_avg_early_season_points_prior_seasons(other_prior)
        ),
        "form_weighted_avg_points": engineering.form_weighted_avg_points(
            current_prior, FORM_HALF_LIFE_GAMEWEEKS
        ),
        "home_avg_points": home_avg_points,
        "away_avg_points": away_avg_points,
        "next_fixture_difficulty": next_fixture_difficulty,
        "next_fixture_was_home": next_fixture_was_home,
        "next_fixture_elo_differential": next_fixture_elo_differential,
        "rest_days": rest_days_value,
        "price_at_time": Decimal(price_at_time) if price_at_time is not None else None,
        "ownership_at_time": ownership_at_time,
        "position_short_name": position_short_name,
        "is_missing_history": is_missing_history,
        "data_source": FEATURE_DATA_SOURCE,
    }


def compute_features_for_player_gameweek(
    session: Session, player_id: uuid.UUID, gameweek_id: uuid.UUID, feature_version: int = FEATURE_VERSION
) -> PlayerFeatures:
    """Compute (but don't add/flush) a single PlayerFeatures row for one player/target-gameweek pair."""
    player = session.get(Player, player_id)
    if player is None:
        raise ValueError(f"player {player_id} not found")
    target_gw = session.get(Gameweek, gameweek_id)
    if target_gw is None:
        raise ValueError(f"gameweek {gameweek_id} not found")

    target_deadline = target_gw.deadline_time

    current_rows = session.execute(
        select(
            PlayerGameweekStats.gameweek_id,
            PlayerGameweekStats.total_points,
            PlayerGameweekStats.minutes,
            PlayerGameweekStats.was_home,
            PlayerGameweekStats.value_at_time,
            PlayerGameweekStats.selected_by_count_at_time,
            PlayerGameweekStats.position_short_name_at_time,
            Gameweek.deadline_time.label("gw_deadline"),
            Gameweek.fpl_event_id,
            Fixture.kickoff_time,
        )
        .join(Gameweek, Gameweek.id == PlayerGameweekStats.gameweek_id)
        .outerjoin(Fixture, Fixture.id == PlayerGameweekStats.fixture_id)
        .where(PlayerGameweekStats.player_id == player_id)
    ).all()

    current_entries_all = [
        PriorGameweekStat(
            season_id=str(player.season_id),
            gameweek_key=str(row.gameweek_id),
            gameweek_deadline=row.gw_deadline,
            total_points=row.total_points,
            minutes=row.minutes,
            was_home=row.was_home,
            fixture_kickoff_time=row.kickoff_time,
            fpl_event_id=row.fpl_event_id,
        )
        for row in current_rows
    ]
    current_prior = engineering.filter_to_strictly_prior(current_entries_all, target_deadline)

    other_prior: list[PriorGameweekStat] = []
    if player.fpl_code is not None:
        other_player_ids = (
            session.execute(
                select(Player.id).where(
                    Player.fpl_code == player.fpl_code, Player.season_id != player.season_id
                )
            )
            .scalars()
            .all()
        )
        if other_player_ids:
            other_rows = session.execute(
                select(
                    PlayerGameweekStats.gameweek_id,
                    PlayerGameweekStats.total_points,
                    PlayerGameweekStats.season_id,
                    Gameweek.deadline_time.label("gw_deadline"),
                    Gameweek.fpl_event_id,
                )
                .join(Gameweek, Gameweek.id == PlayerGameweekStats.gameweek_id)
                .where(PlayerGameweekStats.player_id.in_(other_player_ids))
            ).all()
            other_entries_all = [
                PriorGameweekStat(
                    season_id=str(row.season_id),
                    gameweek_key=str(row.gameweek_id),
                    gameweek_deadline=row.gw_deadline,
                    total_points=row.total_points,
                    minutes=0,
                    was_home=None,
                    fixture_kickoff_time=None,
                    fpl_event_id=row.fpl_event_id,
                )
                for row in other_rows
            ]
            other_prior = engineering.filter_to_strictly_prior(other_entries_all, target_deadline)

    prior_rows_sorted = sorted(
        (row for row in current_rows if row.gw_deadline < target_deadline),
        key=lambda row: (row.gw_deadline, row.kickoff_time or row.gw_deadline),
    )
    most_recent_row = prior_rows_sorted[-1] if prior_rows_sorted else None

    if most_recent_row is not None:
        price_at_time = most_recent_row.value_at_time
        ownership_at_time = most_recent_row.selected_by_count_at_time
        position_short_name = most_recent_row.position_short_name_at_time
    else:
        # No strictly-prior row (this is the player's first tracked gameweek).
        # Prefer this target gameweek's OWN price/ownership if it has a row --
        # a player's price/ownership going into a gameweek's deadline is
        # public, pre-deadline-knowable information (same category as
        # next_fixture_difficulty below), not leakage, and is far more
        # accurate than players.now_cost, which for a historical season is a
        # single end-of-season snapshot (e.g. it would report a player's
        # final price, not their true gameweek-1 price). Only fall back to
        # now_cost when there's no row at all for this gameweek either
        # (e.g. the player didn't feature and has no data anywhere yet).
        target_gw_row = next((row for row in current_rows if row.gameweek_id == gameweek_id), None)
        if target_gw_row is not None:
            price_at_time = target_gw_row.value_at_time
            ownership_at_time = target_gw_row.selected_by_count_at_time
            position_short_name = target_gw_row.position_short_name_at_time
        else:
            price_at_time = player.now_cost
            ownership_at_time = None
            position_short_name = None

    if position_short_name is None and player.position_id is not None:
        position = session.get(Position, player.position_id)
        position_short_name = position.short_name if position is not None else None

    next_fixture = None
    if player.team_id is not None:
        next_fixture = (
            session.execute(
                select(Fixture)
                .where(
                    Fixture.season_id == player.season_id,
                    Fixture.gameweek_id == gameweek_id,
                    (Fixture.home_team_id == player.team_id) | (Fixture.away_team_id == player.team_id),
                )
                .order_by(Fixture.kickoff_time.asc().nullslast())
            )
            .scalars()
            .first()
        )

    if next_fixture is not None:
        is_home = next_fixture.home_team_id == player.team_id
        next_fixture_difficulty = next_fixture.home_difficulty if is_home else next_fixture.away_difficulty
        next_fixture_was_home: bool | None = is_home
        target_fixture_kickoff_time = next_fixture.kickoff_time
        opponent_team_id = next_fixture.away_team_id if is_home else next_fixture.home_team_id
        next_fixture_elo_differential = _resolve_elo_differential(
            session, player.team_id, opponent_team_id
        )
    else:
        next_fixture_difficulty = None
        next_fixture_was_home = None
        target_fixture_kickoff_time = None
        next_fixture_elo_differential = None

    rest_days_value = (
        engineering.rest_days(current_prior, target_fixture_kickoff_time)
        if target_fixture_kickoff_time is not None
        else None
    )

    target_gw_points = [row.total_points for row in current_rows if row.gameweek_id == gameweek_id]

    values = _build_player_features_values(
        player_id=player_id,
        season_id=player.season_id,
        gameweek_id=gameweek_id,
        feature_version=feature_version,
        target_deadline=target_deadline,
        target_finished=target_gw.finished,
        current_prior=current_prior,
        other_prior=other_prior,
        target_gw_points=target_gw_points,
        next_fixture_difficulty=next_fixture_difficulty,
        next_fixture_was_home=next_fixture_was_home,
        next_fixture_elo_differential=next_fixture_elo_differential,
        rest_days_value=rest_days_value,
        price_at_time=price_at_time,
        ownership_at_time=ownership_at_time,
        position_short_name=position_short_name,
        computed_at=datetime.now(UTC),
    )
    return PlayerFeatures(**values)


def generate_features_for_season(
    session: Session, season_id: uuid.UUID, feature_version: int = FEATURE_VERSION
) -> NormalizationCounts:
    """Compute and upsert a PlayerFeatures row for every (player, gameweek) pair in a season.

    Preloads this season's player_gameweek_stats/fixtures and every
    cross-season row reachable via fpl_code once, up front, then computes in
    memory — avoids O(players x gameweeks) round trips to the database.
    Skips quarantined players (same "always exclude quarantined rows from
    active data" convention as everywhere else in this codebase).
    """
    season = session.get(Season, season_id)
    if season is None:
        raise ValueError(f"season {season_id} not found")

    gameweeks = (
        session.execute(
            select(Gameweek).where(Gameweek.season_id == season_id).order_by(Gameweek.deadline_time)
        )
        .scalars()
        .all()
    )
    players = (
        session.execute(select(Player).where(Player.season_id == season_id, Player.is_quarantined.is_(False)))
        .scalars()
        .all()
    )
    if not gameweeks or not players:
        return NormalizationCounts(inserted=0, updated=0, quarantined=0)

    player_ids = [player.id for player in players]

    current_rows = session.execute(
        select(
            PlayerGameweekStats.player_id,
            PlayerGameweekStats.gameweek_id,
            PlayerGameweekStats.total_points,
            PlayerGameweekStats.minutes,
            PlayerGameweekStats.was_home,
            PlayerGameweekStats.value_at_time,
            PlayerGameweekStats.selected_by_count_at_time,
            PlayerGameweekStats.position_short_name_at_time,
            Gameweek.deadline_time.label("gw_deadline"),
            Gameweek.fpl_event_id,
            Fixture.kickoff_time,
        )
        .join(Gameweek, Gameweek.id == PlayerGameweekStats.gameweek_id)
        .outerjoin(Fixture, Fixture.id == PlayerGameweekStats.fixture_id)
        .where(PlayerGameweekStats.player_id.in_(player_ids))
    ).all()

    rows_by_player: dict[uuid.UUID, list[Any]] = defaultdict(list)
    for row in current_rows:
        rows_by_player[row.player_id].append(row)

    # --- Cross-season ("career prior") rows, resolved once via fpl_code ---
    fpl_codes = {player.fpl_code for player in players if player.fpl_code is not None}
    other_stats_by_code: dict[int, list[PriorGameweekStat]] = defaultdict(list)
    if fpl_codes:
        other_players = session.execute(
            select(Player.id, Player.fpl_code).where(
                Player.fpl_code.in_(fpl_codes), Player.season_id != season_id
            )
        ).all()
        other_player_id_to_code = {row.id: row.fpl_code for row in other_players}
        if other_player_id_to_code:
            other_rows = session.execute(
                select(
                    PlayerGameweekStats.player_id,
                    PlayerGameweekStats.gameweek_id,
                    PlayerGameweekStats.total_points,
                    PlayerGameweekStats.season_id,
                    Gameweek.deadline_time.label("gw_deadline"),
                    Gameweek.fpl_event_id,
                )
                .join(Gameweek, Gameweek.id == PlayerGameweekStats.gameweek_id)
                .where(PlayerGameweekStats.player_id.in_(list(other_player_id_to_code.keys())))
            ).all()
            for other_row in other_rows:
                code = other_player_id_to_code[other_row.player_id]
                other_stats_by_code[code].append(
                    PriorGameweekStat(
                        season_id=str(other_row.season_id),
                        gameweek_key=str(other_row.gameweek_id),
                        gameweek_deadline=other_row.gw_deadline,
                        total_points=other_row.total_points,
                        minutes=0,
                        was_home=None,
                        fixture_kickoff_time=None,
                        fpl_event_id=other_row.fpl_event_id,
                    )
                )

    # --- This season's fixtures, indexed for "next fixture" resolution ---
    fixtures = session.execute(select(Fixture).where(Fixture.season_id == season_id)).scalars().all()
    fixture_by_team_gw: dict[tuple[uuid.UUID, uuid.UUID], Fixture] = {}
    for fixture in fixtures:
        if fixture.gameweek_id is None:
            continue
        for team_id in (fixture.home_team_id, fixture.away_team_id):
            if team_id is None:
                continue
            key = (team_id, fixture.gameweek_id)
            existing = fixture_by_team_gw.get(key)
            if existing is None or (
                fixture.kickoff_time is not None
                and (existing.kickoff_time is None or fixture.kickoff_time < existing.kickoff_time)
            ):
                fixture_by_team_gw[key] = fixture

    existing_keys = {
        (player_id, gw_id, fv)
        for player_id, gw_id, fv in session.execute(
            select(
                PlayerFeatures.player_id, PlayerFeatures.gameweek_id, PlayerFeatures.feature_version
            ).where(PlayerFeatures.season_id == season_id, PlayerFeatures.feature_version == feature_version)
        )
    }

    # Season-level Position fallback (used below whenever no prior stat row
    # carried a position at all, e.g. every target gameweek is this player's
    # first-ever gameweek) — the positions table is tiny (4-5 rows/season),
    # so preloading all of it up front is cheap and avoids any per-row query.
    position_short_name_by_id: dict[uuid.UUID, str] = {
        position.id: position.short_name
        for position in session.execute(select(Position).where(Position.season_id == season_id)).scalars()
    }

    # Same "preload once, look up in memory" precedent as position_short_name_by_id above --
    # teams is also a tiny per-season table. Only teams with a real synced elo_rating appear
    # (see app.jobs.handlers.team_strength_sync); missing from this dict == unknown, not zero.
    elo_by_team_id: dict[uuid.UUID, Decimal] = {
        team.id: team.elo_rating
        for team in session.execute(select(Team).where(Team.season_id == season_id)).scalars()
        if team.elo_rating is not None
    }

    computed_at = datetime.now(UTC)
    all_values: list[dict[str, Any]] = []

    for player in players:
        player_rows = rows_by_player.get(player.id, [])
        current_entries_all = [
            PriorGameweekStat(
                season_id=str(season_id),
                gameweek_key=str(row.gameweek_id),
                gameweek_deadline=row.gw_deadline,
                total_points=row.total_points,
                minutes=row.minutes,
                was_home=row.was_home,
                fixture_kickoff_time=row.kickoff_time,
                fpl_event_id=row.fpl_event_id,
            )
            for row in player_rows
        ]
        other_entries_all = (
            other_stats_by_code.get(player.fpl_code, []) if player.fpl_code is not None else []
        )

        points_by_gw: dict[uuid.UUID, list[int]] = defaultdict(list)
        for row in player_rows:
            points_by_gw[row.gameweek_id].append(row.total_points)

        for gameweek in gameweeks:
            target_deadline = gameweek.deadline_time
            current_prior = engineering.filter_to_strictly_prior(current_entries_all, target_deadline)
            other_prior = engineering.filter_to_strictly_prior(other_entries_all, target_deadline)

            prior_rows_sorted = sorted(
                (row for row in player_rows if row.gw_deadline < target_deadline),
                key=lambda row: (row.gw_deadline, row.kickoff_time or row.gw_deadline),
            )
            most_recent_row = prior_rows_sorted[-1] if prior_rows_sorted else None
            if most_recent_row is not None:
                price_at_time = most_recent_row.value_at_time
                ownership_at_time = most_recent_row.selected_by_count_at_time
                position_short_name = most_recent_row.position_short_name_at_time
            else:
                # See compute_features_for_player_gameweek's identical fallback
                # for the full rationale: prefer this gameweek's own row (its
                # price/ownership is pre-deadline-knowable, not leakage) over
                # players.now_cost's stale end-of-season snapshot.
                target_gw_row = next((row for row in player_rows if row.gameweek_id == gameweek.id), None)
                if target_gw_row is not None:
                    price_at_time = target_gw_row.value_at_time
                    ownership_at_time = target_gw_row.selected_by_count_at_time
                    position_short_name = target_gw_row.position_short_name_at_time
                else:
                    price_at_time = player.now_cost
                    ownership_at_time = None
                    position_short_name = None

            if position_short_name is None and player.position_id is not None:
                position_short_name = position_short_name_by_id.get(player.position_id)

            next_fixture = fixture_by_team_gw.get((player.team_id, gameweek.id)) if player.team_id else None
            if next_fixture is not None:
                is_home = next_fixture.home_team_id == player.team_id
                next_fixture_difficulty = (
                    next_fixture.home_difficulty if is_home else next_fixture.away_difficulty
                )
                next_fixture_was_home: bool | None = is_home
                target_fixture_kickoff_time = next_fixture.kickoff_time
                opponent_team_id = next_fixture.away_team_id if is_home else next_fixture.home_team_id
                team_elo = elo_by_team_id.get(player.team_id) if player.team_id else None
                opponent_elo = elo_by_team_id.get(opponent_team_id) if opponent_team_id else None
                next_fixture_elo_differential = (
                    team_elo - opponent_elo if team_elo is not None and opponent_elo is not None else None
                )
            else:
                next_fixture_difficulty = None
                next_fixture_was_home = None
                target_fixture_kickoff_time = None
                next_fixture_elo_differential = None

            rest_days_value = (
                engineering.rest_days(current_prior, target_fixture_kickoff_time)
                if target_fixture_kickoff_time is not None
                else None
            )

            values = _build_player_features_values(
                player_id=player.id,
                season_id=season_id,
                gameweek_id=gameweek.id,
                feature_version=feature_version,
                target_deadline=target_deadline,
                target_finished=gameweek.finished,
                current_prior=current_prior,
                other_prior=other_prior,
                target_gw_points=points_by_gw.get(gameweek.id, []),
                next_fixture_difficulty=next_fixture_difficulty,
                next_fixture_was_home=next_fixture_was_home,
                next_fixture_elo_differential=next_fixture_elo_differential,
                rest_days_value=rest_days_value,
                price_at_time=price_at_time,
                ownership_at_time=ownership_at_time,
                position_short_name=position_short_name,
                computed_at=computed_at,
            )

            all_values.append(values)

    inserted, updated = _bulk_upsert(
        session,
        PlayerFeatures,
        all_values,
        index_elements=["player_id", "gameweek_id", "feature_version"],
        existing_keys=existing_keys,
        key_fields=["player_id", "gameweek_id", "feature_version"],
    )
    return NormalizationCounts(inserted=inserted, updated=updated, quarantined=0)
