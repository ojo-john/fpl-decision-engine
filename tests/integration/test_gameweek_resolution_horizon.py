"""Integration tests for app.optimization.gameweek_resolution.resolve_horizon_gameweeks.

Needs a real Postgres session (Gameweek is a genuine ORM/DB-querying
function) -- mirrors this repo's require_database/db_session convention
(see tests/integration/conftest.py). Builds Season/Gameweek rows directly
via the ORM rather than through the full ingestion pipeline: this function
only reads season_id/deadline_time/finished, so a full DATA_INGESTION run
would be unnecessary overhead for testing pure gameweek-ordering logic.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.optimization.gameweek_resolution import resolve_horizon_gameweeks
from packages.database.models.gameweek import Gameweek
from packages.database.models.season import Season


def _make_season(db_session) -> uuid.UUID:
    season = Season(label=f"hzn-{uuid.uuid4().hex[:12]}", is_current=False)
    db_session.add(season)
    db_session.commit()
    db_session.refresh(season)
    return season.id


def _make_gameweek(
    db_session, season_id: uuid.UUID, fpl_event_id: int, deadline_offset_days: int, finished: bool
) -> uuid.UUID:
    gameweek = Gameweek(
        fpl_event_id=fpl_event_id,
        season_id=season_id,
        name=f"Gameweek {fpl_event_id}",
        deadline_time=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=deadline_offset_days),
        finished=finished,
        last_synced_at=datetime.now(UTC),
    )
    db_session.add(gameweek)
    db_session.commit()
    db_session.refresh(gameweek)
    return gameweek.id


def test_resolve_horizon_gameweeks_returns_correct_count_and_order(db_session):
    season_id = _make_season(db_session)
    # Two finished (in the past), then 6 unfinished, ascending deadline order.
    _make_gameweek(db_session, season_id, 1, deadline_offset_days=-14, finished=True)
    _make_gameweek(db_session, season_id, 2, deadline_offset_days=-7, finished=True)
    unfinished_ids = [
        _make_gameweek(db_session, season_id, event_id, deadline_offset_days=event_id * 7, finished=False)
        for event_id in range(3, 9)  # events 3..8, six unfinished gameweeks
    ]

    result = resolve_horizon_gameweeks(db_session, season_id, horizon_length=4)

    assert result == unfinished_ids[:4]


def test_resolve_horizon_gameweeks_truncates_gracefully_near_season_end(db_session):
    season_id = _make_season(db_session)
    _make_gameweek(db_session, season_id, 1, deadline_offset_days=-7, finished=True)
    unfinished_ids = [
        _make_gameweek(db_session, season_id, event_id, deadline_offset_days=event_id * 7, finished=False)
        for event_id in range(2, 5)  # only 3 unfinished gameweeks left
    ]

    result = resolve_horizon_gameweeks(db_session, season_id, horizon_length=10)

    assert result == unfinished_ids
    assert len(result) == 3


def test_resolve_horizon_gameweeks_returns_empty_list_when_all_finished(db_session):
    season_id = _make_season(db_session)
    for event_id in range(1, 6):
        _make_gameweek(db_session, season_id, event_id, deadline_offset_days=event_id * 7, finished=True)

    result = resolve_horizon_gameweeks(db_session, season_id, horizon_length=5)

    assert result == []


def test_resolve_horizon_gameweeks_empty_for_season_with_no_gameweeks_at_all(db_session):
    season_id = _make_season(db_session)

    result = resolve_horizon_gameweeks(db_session, season_id, horizon_length=5)

    assert result == []
    # sanity: this season really has zero gameweek rows, not a query bug
    assert db_session.execute(select(Gameweek).where(Gameweek.season_id == season_id)).first() is None


@pytest.mark.parametrize("horizon_length", [0, -1])
def test_resolve_horizon_gameweeks_rejects_non_positive_horizon_length(db_session, horizon_length):
    """A non-positive horizon_length is always a misconfiguration (e.g. a bad hand-maintained
    constant), never a legitimate request -- it must raise, not silently return [] and get
    confused with the genuine off-season no-active-gameweek state.
    """
    season_id = _make_season(db_session)
    _make_gameweek(db_session, season_id, 1, deadline_offset_days=7, finished=False)

    with pytest.raises(ValueError):
        resolve_horizon_gameweeks(db_session, season_id, horizon_length=horizon_length)
