"""Integration tests for app.optimization.gameweek_resolution's public functions:
resolve_horizon_gameweeks, resolve_target_gameweek, and resolve_gameweeks_within_window.

Needs a real Postgres session (Gameweek is a genuine ORM/DB-querying
function) -- mirrors this repo's require_database/db_session convention
(see tests/integration/conftest.py). Builds Season/Gameweek rows directly
via the ORM rather than through the full ingestion pipeline: these functions
only read season_id/deadline_time/fpl_event_id, so a full DATA_INGESTION run
would be unnecessary overhead for testing pure gameweek-ordering logic.

``_NOW`` is a fixed, arbitrary reference point (NOT real wall-clock time) --
every function under test takes an explicit ``now: datetime`` parameter
rather than calling ``datetime.now(UTC)`` internally, specifically so these
tests can be fully deterministic regardless of when they're actually run.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.optimization.gameweek_resolution import (
    resolve_gameweeks_within_window,
    resolve_horizon_gameweeks,
    resolve_target_gameweek,
)
from packages.database.models.gameweek import Gameweek
from packages.database.models.season import Season
from packages.domain.optimization import TargetGameweekStatus

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


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
        deadline_time=_NOW + timedelta(days=deadline_offset_days),
        finished=finished,
        last_synced_at=datetime.now(UTC),
    )
    db_session.add(gameweek)
    db_session.commit()
    db_session.refresh(gameweek)
    return gameweek.id


# --- resolve_horizon_gameweeks -----------------------------------------------


def test_resolve_horizon_gameweeks_returns_correct_count_and_order(db_session):
    season_id = _make_season(db_session)
    # Two with passed deadlines (in the past), then 6 with open deadlines, ascending order.
    _make_gameweek(db_session, season_id, 1, deadline_offset_days=-14, finished=True)
    _make_gameweek(db_session, season_id, 2, deadline_offset_days=-7, finished=True)
    actionable_ids = [
        _make_gameweek(db_session, season_id, event_id, deadline_offset_days=event_id * 7, finished=False)
        for event_id in range(3, 9)  # events 3..8, six with open deadlines
    ]

    result = resolve_horizon_gameweeks(db_session, season_id, horizon_length=4, now=_NOW)

    assert result == actionable_ids[:4]


def test_resolve_horizon_gameweeks_truncates_gracefully_near_season_end(db_session):
    season_id = _make_season(db_session)
    _make_gameweek(db_session, season_id, 1, deadline_offset_days=-7, finished=True)
    actionable_ids = [
        _make_gameweek(db_session, season_id, event_id, deadline_offset_days=event_id * 7, finished=False)
        for event_id in range(2, 5)  # only 3 gameweeks left with open deadlines
    ]

    result = resolve_horizon_gameweeks(db_session, season_id, horizon_length=10, now=_NOW)

    assert result == actionable_ids
    assert len(result) == 3


def test_resolve_horizon_gameweeks_returns_empty_list_when_all_deadlines_passed(db_session):
    season_id = _make_season(db_session)
    for event_id in range(1, 6):
        _make_gameweek(db_session, season_id, event_id, deadline_offset_days=-event_id * 7, finished=True)

    result = resolve_horizon_gameweeks(db_session, season_id, horizon_length=5, now=_NOW)

    assert result == []


def test_resolve_horizon_gameweeks_empty_for_season_with_no_gameweeks_at_all(db_session):
    season_id = _make_season(db_session)

    result = resolve_horizon_gameweeks(db_session, season_id, horizon_length=5, now=_NOW)

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
        resolve_horizon_gameweeks(db_session, season_id, horizon_length=horizon_length, now=_NOW)


def test_resolve_horizon_gameweeks_excludes_deadline_passed_but_not_yet_finished_gameweek(db_session):
    """Real bug, confirmed live (2026-08-22): a gameweek's ``finished`` flag only flips once
    every match has been PLAYED, days after its deadline -- so a gameweek whose deadline has
    passed but hasn't finished playing yet must still be excluded from the horizon. Filtering on
    ``finished=False`` alone (the pre-fix behavior) would incorrectly include it.
    """
    season_id = _make_season(db_session)
    deadline_passed_not_finished = _make_gameweek(
        db_session, season_id, 1, deadline_offset_days=-1, finished=False
    )
    actionable_ids = [
        _make_gameweek(db_session, season_id, event_id, deadline_offset_days=event_id * 7, finished=False)
        for event_id in range(2, 5)
    ]

    result = resolve_horizon_gameweeks(db_session, season_id, horizon_length=10, now=_NOW)

    assert deadline_passed_not_finished not in result
    assert result == actionable_ids


# --- resolve_target_gameweek --------------------------------------------------


def test_resolve_target_gameweek_returns_earliest_actionable_by_deadline(db_session):
    season_id = _make_season(db_session)
    _make_gameweek(db_session, season_id, 1, deadline_offset_days=-7, finished=True)
    earliest_actionable = _make_gameweek(db_session, season_id, 2, deadline_offset_days=1, finished=False)
    _make_gameweek(db_session, season_id, 3, deadline_offset_days=8, finished=False)

    gameweek_id, status = resolve_target_gameweek(db_session, season_id, now=_NOW)

    assert status == TargetGameweekStatus.RESOLVED
    assert gameweek_id == earliest_actionable


def test_resolve_target_gameweek_excludes_deadline_passed_but_not_yet_finished_gameweek(db_session):
    """Same real bug as the horizon test above, pinned for the single-gameweek resolver too --
    this is the exact function the live recommendation-generation bug traced back to."""
    season_id = _make_season(db_session)
    _make_gameweek(db_session, season_id, 1, deadline_offset_days=-1, finished=False)
    next_actionable = _make_gameweek(db_session, season_id, 2, deadline_offset_days=6, finished=False)

    gameweek_id, status = resolve_target_gameweek(db_session, season_id, now=_NOW)

    assert status == TargetGameweekStatus.RESOLVED
    assert gameweek_id == next_actionable


def test_resolve_target_gameweek_no_active_gameweek_when_every_deadline_passed(db_session):
    season_id = _make_season(db_session)
    _make_gameweek(db_session, season_id, 1, deadline_offset_days=-14, finished=True)
    _make_gameweek(db_session, season_id, 2, deadline_offset_days=-1, finished=False)

    gameweek_id, status = resolve_target_gameweek(db_session, season_id, now=_NOW)

    assert status == TargetGameweekStatus.NO_ACTIVE_GAMEWEEK
    assert gameweek_id is None


# --- resolve_gameweeks_within_window ------------------------------------------


def test_resolve_gameweeks_within_window_bounds_by_stop_event(db_session):
    season_id = _make_season(db_session)
    gw2 = _make_gameweek(db_session, season_id, 2, deadline_offset_days=1, finished=False)
    gw3 = _make_gameweek(db_session, season_id, 3, deadline_offset_days=8, finished=False)
    _make_gameweek(db_session, season_id, 4, deadline_offset_days=15, finished=False)

    result = resolve_gameweeks_within_window(db_session, season_id, stop_event=3, now=_NOW)

    assert result == [gw2, gw3]


def test_resolve_gameweeks_within_window_excludes_deadline_passed_but_not_yet_finished_gameweek(db_session):
    season_id = _make_season(db_session)
    _make_gameweek(db_session, season_id, 1, deadline_offset_days=-1, finished=False)
    gw2 = _make_gameweek(db_session, season_id, 2, deadline_offset_days=1, finished=False)

    result = resolve_gameweeks_within_window(db_session, season_id, stop_event=2, now=_NOW)

    assert result == [gw2]


def test_resolve_gameweeks_within_window_empty_when_window_already_closed(db_session):
    season_id = _make_season(db_session)
    _make_gameweek(db_session, season_id, 1, deadline_offset_days=-7, finished=True)

    result = resolve_gameweeks_within_window(db_session, season_id, stop_event=1, now=_NOW)

    assert result == []


def test_resolve_gameweeks_within_window_start_event_excludes_an_earlier_still_open_window(db_session):
    """The real gap found building the joint chip scheduler's Phase 2: without an explicit
    ``start_event`` lower bound, resolving a FUTURE window (e.g. window 2, GW20-38) while the
    season is still inside an earlier one (window 1, GW1-19) would silently also return every
    remaining gameweek of that earlier window too, since ``deadline_time > now`` alone doesn't
    know anything about window boundaries. All 3 seeded gameweeks here have open deadlines --
    only ``start_event`` distinguishes "window 1's own remaining gameweeks" from "window 2"."""
    season_id = _make_season(db_session)
    _make_gameweek(db_session, season_id, 5, deadline_offset_days=1, finished=False)
    gw20 = _make_gameweek(db_session, season_id, 20, deadline_offset_days=100, finished=False)
    gw21 = _make_gameweek(db_session, season_id, 21, deadline_offset_days=107, finished=False)

    result = resolve_gameweeks_within_window(db_session, season_id, stop_event=38, now=_NOW, start_event=20)

    assert result == [gw20, gw21]
