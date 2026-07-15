"""Integration tests for app.optimization.fixture_calendar.

Needs a real Postgres session (Fixture/Team/Gameweek are genuine ORM/DB
querying) -- mirrors this repo's require_database/db_session convention (see
tests/integration/conftest.py) and
tests/integration/test_gameweek_resolution_horizon.py's style of building
Season/Team/Gameweek/Fixture rows directly via the ORM rather than through
the full ingestion pipeline.

Hand-builds a small synthetic season with a genuine Double Gameweek pattern
(one team plays twice), a Blank Gameweek team (zero fixtures), and a
quarantined fixture that must be excluded entirely from both sides' counts.
"""

import uuid
from datetime import UTC, datetime

from app.optimization.fixture_calendar import (
    TeamFixtureCount,
    resolve_team_fixture_counts_for_gameweek,
    resolve_team_fixture_counts_for_gameweeks,
)
from packages.database.models.fixture import Fixture
from packages.database.models.gameweek import Gameweek
from packages.database.models.season import Season
from packages.database.models.team import Team


def _make_season(db_session) -> uuid.UUID:
    season = Season(label=f"fxc-{uuid.uuid4().hex[:12]}", is_current=False)
    db_session.add(season)
    db_session.commit()
    db_session.refresh(season)
    return season.id


def _make_team(db_session, season_id: uuid.UUID, fpl_team_id: int, short_name: str) -> uuid.UUID:
    team = Team(
        fpl_team_id=fpl_team_id,
        season_id=season_id,
        name=f"Team {short_name}",
        short_name=short_name,
        last_synced_at=datetime.now(UTC),
    )
    db_session.add(team)
    db_session.commit()
    db_session.refresh(team)
    return team.id


def _make_gameweek(db_session, season_id: uuid.UUID, fpl_event_id: int) -> uuid.UUID:
    gameweek = Gameweek(
        fpl_event_id=fpl_event_id,
        season_id=season_id,
        name=f"Gameweek {fpl_event_id}",
        deadline_time=datetime(2026, 1, 1, tzinfo=UTC),
        finished=False,
        last_synced_at=datetime.now(UTC),
    )
    db_session.add(gameweek)
    db_session.commit()
    db_session.refresh(gameweek)
    return gameweek.id


def _make_fixture(
    db_session,
    season_id: uuid.UUID,
    gameweek_id: uuid.UUID | None,
    fpl_fixture_id: int,
    home_team_id: uuid.UUID | None,
    away_team_id: uuid.UUID | None,
    is_quarantined: bool = False,
) -> uuid.UUID:
    fixture = Fixture(
        fpl_fixture_id=fpl_fixture_id,
        season_id=season_id,
        gameweek_id=gameweek_id,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        finished=False,
        started=False,
        is_quarantined=is_quarantined,
        last_synced_at=datetime.now(UTC),
    )
    db_session.add(fixture)
    db_session.commit()
    db_session.refresh(fixture)
    return fixture.id


def test_resolve_team_fixture_counts_for_gameweek_handles_double_blank_and_quarantine(db_session):
    season_id = _make_season(db_session)
    team_a = _make_team(db_session, season_id, 9001, "AAA")  # double gameweek
    team_b = _make_team(db_session, season_id, 9002, "BBB")  # normal, 1 fixture
    team_c = _make_team(db_session, season_id, 9003, "CCC")  # blank -- zero fixtures at all
    team_d = _make_team(db_session, season_id, 9004, "DDD")  # normal, 1 fixture
    team_e = _make_team(db_session, season_id, 9005, "EEE")  # only fixture is quarantined
    team_f = _make_team(db_session, season_id, 9006, "FFF")  # only fixture is quarantined

    gw = _make_gameweek(db_session, season_id, 10)

    fixture_ab = _make_fixture(db_session, season_id, gw, 50001, team_a, team_b)
    fixture_da = _make_fixture(db_session, season_id, gw, 50002, team_d, team_a)
    _make_fixture(db_session, season_id, gw, 50003, team_e, team_f, is_quarantined=True)

    result = resolve_team_fixture_counts_for_gameweek(db_session, season_id, gw)

    assert result[team_a] == TeamFixtureCount(team_a, 2, (fixture_ab, fixture_da))
    assert result[team_b] == TeamFixtureCount(team_b, 1, (fixture_ab,))
    assert result[team_c] == TeamFixtureCount(team_c, 0, ())  # blank, but still present in the dict
    assert result[team_d] == TeamFixtureCount(team_d, 1, (fixture_da,))
    assert result[team_e] == TeamFixtureCount(team_e, 0, ())  # only fixture excluded (quarantined)
    assert result[team_f] == TeamFixtureCount(team_f, 0, ())
    assert set(result.keys()) == {team_a, team_b, team_c, team_d, team_e, team_f}


def test_resolve_team_fixture_counts_for_gameweeks_batches_across_multiple_gameweeks(db_session):
    season_id = _make_season(db_session)
    team_a = _make_team(db_session, season_id, 9101, "AAA")
    team_b = _make_team(db_session, season_id, 9102, "BBB")

    gw1 = _make_gameweek(db_session, season_id, 20)
    gw2 = _make_gameweek(db_session, season_id, 21)

    fixture_gw1 = _make_fixture(db_session, season_id, gw1, 51001, team_a, team_b)
    fixture_gw2 = _make_fixture(db_session, season_id, gw2, 51002, team_b, team_a)

    result = resolve_team_fixture_counts_for_gameweeks(db_session, season_id, [gw1, gw2])

    assert set(result.keys()) == {gw1, gw2}
    assert result[gw1][team_a] == TeamFixtureCount(team_a, 1, (fixture_gw1,))
    assert result[gw1][team_b] == TeamFixtureCount(team_b, 1, (fixture_gw1,))
    assert result[gw2][team_a] == TeamFixtureCount(team_a, 1, (fixture_gw2,))
    assert result[gw2][team_b] == TeamFixtureCount(team_b, 1, (fixture_gw2,))


def test_resolve_team_fixture_counts_excludes_fixture_with_null_gameweek_id(db_session):
    """A postponed/TBD fixture (gameweek_id=NULL per Fixture's own docstring) must not be
    attributed to any gameweek's count -- known, accepted freshness limitation.
    """
    season_id = _make_season(db_session)
    team_a = _make_team(db_session, season_id, 9201, "AAA")
    team_b = _make_team(db_session, season_id, 9202, "BBB")
    gw = _make_gameweek(db_session, season_id, 30)

    _make_fixture(db_session, season_id, None, 52001, team_a, team_b)  # unresolved gameweek

    result = resolve_team_fixture_counts_for_gameweek(db_session, season_id, gw)

    assert result[team_a] == TeamFixtureCount(team_a, 0, ())
    assert result[team_b] == TeamFixtureCount(team_b, 0, ())
