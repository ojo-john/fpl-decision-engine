"""Integration tests for Milestone 9 Layer 3: DEADLINE_REMINDER, against a real database.

Real dedup gap this closes: FPL's own gameweek numbers (1-38) reset every season, so the dedup
check must be scoped by season, not just fpl_event_id (see app.jobs.handlers.deadline_reminder's
own docstring on `_already_reminded`) -- these prove the "second run doesn't repeat" checkpoint the
plan asked for, and that a FRESH season's same gameweek number is unaffected by another season's
already-reminded row.

Shared, never-truncated Postgres (see conftest.py's own module docstring): every test that cares
about `Season.is_current` state explicitly resets it first rather than assuming a pristine DB --
same established convention as tests/integration/test_api_chip_windows.py's own `_reset` pattern.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import update

from app.jobs.service import execute_job
from packages.database.models.gameweek import Gameweek
from packages.database.models.season import Season
from packages.domain.jobs import JobStatus, JobType, TriggerType


def _reset_current_season(db_session) -> None:
    db_session.execute(update(Season).values(is_current=False))
    db_session.commit()


def _make_current_season(db_session) -> uuid.UUID:
    _reset_current_season(db_session)
    season = Season(label=f"dr-{uuid.uuid4().hex[:12]}", is_current=True)
    db_session.add(season)
    db_session.commit()
    db_session.refresh(season)
    return season.id


def _make_gameweek(db_session, season_id: uuid.UUID, fpl_event_id: int, deadline_time: datetime) -> None:
    db_session.add(
        Gameweek(
            fpl_event_id=fpl_event_id,
            season_id=season_id,
            name=f"Gameweek {fpl_event_id}",
            deadline_time=deadline_time,
            finished=False,
            last_synced_at=datetime.now(UTC),
        )
    )
    db_session.commit()


def _capture_alerts(monkeypatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "app.jobs.handlers.deadline_reminder.send_push_to_all",
        lambda session, title, body, url=None: calls.append((title, body)) or 0,
    )
    return calls


def _run(db_session):
    return execute_job(db_session, job_type=JobType.DEADLINE_REMINDER, trigger_type=TriggerType.LOCAL)


def test_no_current_season_is_a_noop_success(db_session):
    _reset_current_season(db_session)

    job = _run(db_session)

    assert job.status == JobStatus.SUCCEEDED
    assert job.output_json == {"reminder_sent": False, "reason": "NO_CURRENT_SEASON"}


def test_no_actionable_gameweek_is_a_noop_success(db_session):
    _make_current_season(db_session)  # no gameweeks at all for this fresh season

    job = _run(db_session)

    assert job.status == JobStatus.SUCCEEDED
    assert job.output_json == {"reminder_sent": False, "reason": "no_active_gameweek"}


def test_deadline_outside_window_is_a_noop_success(db_session):
    season_id = _make_current_season(db_session)
    deadline = datetime.now(UTC) + timedelta(hours=12)
    _make_gameweek(db_session, season_id, fpl_event_id=1, deadline_time=deadline)

    job = _run(db_session)

    assert job.status == JobStatus.SUCCEEDED
    assert job.output_json["reminder_sent"] is False
    assert job.output_json["reason"] == "OUTSIDE_WINDOW"
    assert job.output_json["target_fpl_event_id"] == 1


def test_deadline_within_window_sends_exactly_one_alert(db_session, monkeypatch):
    calls = _capture_alerts(monkeypatch)
    season_id = _make_current_season(db_session)
    deadline = datetime.now(UTC) + timedelta(hours=2)
    _make_gameweek(db_session, season_id, fpl_event_id=1, deadline_time=deadline)

    job = _run(db_session)

    assert job.status == JobStatus.SUCCEEDED
    assert job.output_json["reminder_sent"] is True
    assert job.output_json["target_fpl_event_id"] == 1
    assert job.output_json["target_season_id"] == str(season_id)
    assert len(calls) == 1
    assert calls[0][0] == "GW1 deadline in 2.0h"


def test_second_run_for_the_same_gameweek_does_not_repeat(db_session, monkeypatch):
    calls = _capture_alerts(monkeypatch)
    season_id = _make_current_season(db_session)
    deadline = datetime.now(UTC) + timedelta(hours=2)
    _make_gameweek(db_session, season_id, fpl_event_id=1, deadline_time=deadline)

    _run(db_session)
    second = _run(db_session)

    assert second.status == JobStatus.SUCCEEDED
    assert second.output_json["reminder_sent"] is False
    assert second.output_json["reason"] == "ALREADY_REMINDED"
    assert len(calls) == 1


def test_same_gameweek_number_in_a_different_season_is_not_suppressed(db_session, monkeypatch):
    """Real bug this guards: FPL's own fpl_event_id values (1-38) reset every season -- a naive
    fpl_event_id-only dedup check would let this first season's GW1 reminder permanently suppress
    the next season's GW1 reminder too."""
    calls = _capture_alerts(monkeypatch)
    first_season_id = _make_current_season(db_session)
    deadline = datetime.now(UTC) + timedelta(hours=2)
    _make_gameweek(db_session, first_season_id, fpl_event_id=1, deadline_time=deadline)
    _run(db_session)
    assert len(calls) == 1

    # _make_current_season resets is_current, making this the new current season.
    second_season_id = _make_current_season(db_session)
    _make_gameweek(db_session, second_season_id, fpl_event_id=1, deadline_time=deadline)

    job = _run(db_session)

    assert job.status == JobStatus.SUCCEEDED
    assert job.output_json["reminder_sent"] is True
    assert job.output_json["target_season_id"] == str(second_season_id)
    assert len(calls) == 2
