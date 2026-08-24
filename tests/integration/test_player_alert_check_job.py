"""Integration tests for Milestone 9 Layer 4: PLAYER_ALERT_CHECK, against a real database.

Checkpoint the plan asked for: "a player whose status flips from 'a' to 'i' between two ingestion
runs produces exactly one push and one updated PlayerAlertState row" -- see
test_status_change_from_available_to_injured_sends_exactly_one_alert below.

Shared, never-truncated Postgres (see conftest.py's own module docstring): every test that cares
about `Season.is_current` state explicitly resets it first, same established convention as
tests/integration/test_api_chip_windows.py's own `_reset` pattern (also reused by
test_deadline_reminder_job.py).
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select, update

from app.jobs.service import execute_job
from packages.database.models.player import Player
from packages.database.models.player_alert_state import PlayerAlertState
from packages.database.models.season import Season
from packages.domain.jobs import JobStatus, JobType, TriggerType

# Chosen so net transfer activity clears BOTH classify_price_momentum's default thresholds
# (ratio >= 2%, absolute >= 1000) -- see app.players.price_signal's own DEFAULT_* constants.
_TOTAL_PLAYERS = 1_000_000
_SELECTED_BY_PERCENT = Decimal("10.0")  # -> estimated_owners = 100,000
_RISING_TRANSFERS_IN = 5_000  # net 5,000 / 100,000 = 5% >= 2%, and >= 1000 absolute


def _reset_current_season(db_session) -> None:
    db_session.execute(update(Season).values(is_current=False))
    db_session.commit()


def _make_current_season(db_session) -> uuid.UUID:
    _reset_current_season(db_session)
    season = Season(label=f"pac-{uuid.uuid4().hex[:12]}", is_current=True, total_players=_TOTAL_PLAYERS)
    db_session.add(season)
    db_session.commit()
    db_session.refresh(season)
    return season.id


def _make_player(
    db_session,
    season_id: uuid.UUID,
    *,
    status: str | None = "a",
    transfers_in_event: int = 0,
    transfers_out_event: int = 0,
    web_name: str = "Test Player",
) -> Player:
    player = Player(
        fpl_element_id=100_000 + uuid.uuid4().int % 100_000,
        season_id=season_id,
        now_cost=50,
        web_name=web_name,
        status=status,
        selected_by_percent=_SELECTED_BY_PERCENT,
        transfers_in_event=transfers_in_event,
        transfers_out_event=transfers_out_event,
        last_synced_at=datetime.now(UTC),
    )
    db_session.add(player)
    db_session.commit()
    db_session.refresh(player)
    return player


def _capture_alerts(monkeypatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "app.jobs.handlers.player_alert_check.send_push_to_all",
        lambda session, title, body, url=None: calls.append((title, body)) or 0,
    )
    return calls


def _run(db_session):
    return execute_job(db_session, job_type=JobType.PLAYER_ALERT_CHECK, trigger_type=TriggerType.LOCAL)


def _state_for(db_session, player_id: uuid.UUID) -> PlayerAlertState:
    return db_session.execute(
        select(PlayerAlertState).where(PlayerAlertState.player_id == player_id)
    ).scalar_one()


def test_no_current_season_is_a_noop_success(db_session):
    _reset_current_season(db_session)

    job = _run(db_session)

    assert job.status == JobStatus.SUCCEEDED
    assert job.output_json == {"players_checked": 0, "alerts_sent": 0, "reason": "NO_CURRENT_SEASON"}


def test_first_check_seeds_baseline_without_alerting(db_session, monkeypatch):
    calls = _capture_alerts(monkeypatch)
    season_id = _make_current_season(db_session)
    player = _make_player(db_session, season_id, status="a")

    job = _run(db_session)

    assert job.status == JobStatus.SUCCEEDED
    assert job.output_json["alerts_sent"] == 0
    assert len(calls) == 0
    state = _state_for(db_session, player.id)
    assert state.status == "a"


def test_status_change_from_available_to_injured_sends_exactly_one_alert(db_session, monkeypatch):
    calls = _capture_alerts(monkeypatch)
    season_id = _make_current_season(db_session)
    player = _make_player(db_session, season_id, status="a", web_name="Injury Test Player")

    _run(db_session)  # seeds the baseline (status="a"), no alert yet
    assert len(calls) == 0

    player.status = "i"
    db_session.commit()

    job = _run(db_session)

    assert job.status == JobStatus.SUCCEEDED
    assert job.output_json["alerts_sent"] == 1
    assert len(calls) == 1
    assert "Injury Test Player" in calls[0][0]
    state = _state_for(db_session, player.id)
    assert state.status == "i"


def test_unchanged_status_sends_no_further_alert(db_session, monkeypatch):
    calls = _capture_alerts(monkeypatch)
    season_id = _make_current_season(db_session)
    _make_player(db_session, season_id, status="i")

    _run(db_session)  # seeds baseline at status="i"
    second = _run(db_session)  # status is still "i" -- nothing changed

    assert second.output_json["alerts_sent"] == 0
    assert len(calls) == 0


def test_price_momentum_change_sends_an_alert(db_session, monkeypatch):
    calls = _capture_alerts(monkeypatch)
    season_id = _make_current_season(db_session)
    player = _make_player(db_session, season_id, transfers_in_event=0, transfers_out_event=0)

    _run(db_session)  # seeds baseline at momentum="stable"
    assert len(calls) == 0

    player.transfers_in_event = _RISING_TRANSFERS_IN
    db_session.commit()

    job = _run(db_session)

    assert job.output_json["alerts_sent"] == 1
    assert len(calls) == 1
    state = _state_for(db_session, player.id)
    assert state.price_momentum == "likely_rise"


def test_failed_send_leaves_state_un_upserted_for_retry(db_session, monkeypatch):
    """Per-player isolation: a failed push must not be recorded as delivered, or the same real
    change would never be retried and the manager would never actually find out."""
    season_id = _make_current_season(db_session)
    player = _make_player(db_session, season_id, status="a", web_name="Failing Push Player")
    _run(db_session)  # seeds baseline at status="a"

    def _raise(session, title, body, url=None):
        raise RuntimeError("push service unreachable")

    monkeypatch.setattr("app.jobs.handlers.player_alert_check.send_push_to_all", _raise)
    player.status = "i"
    db_session.commit()

    job = _run(db_session)

    assert job.status == JobStatus.SUCCEEDED  # one player's failed send doesn't fail the job
    assert job.output_json["alerts_sent"] == 0
    state = _state_for(db_session, player.id)
    assert state.status == "a"  # NOT advanced to "i" -- next run will retry this same diff


def test_other_players_are_unaffected_when_one_players_send_fails(db_session, monkeypatch):
    season_id = _make_current_season(db_session)
    failing_player = _make_player(db_session, season_id, status="a", web_name="Failing Player")
    healthy_player = _make_player(db_session, season_id, status="a", web_name="Healthy Player")
    _run(db_session)  # seeds baselines for both

    def _raise_only_for_failing_player(session, title, body, url=None):
        if "Failing Player" in title:
            raise RuntimeError("push service unreachable")
        return 0

    monkeypatch.setattr(
        "app.jobs.handlers.player_alert_check.send_push_to_all", _raise_only_for_failing_player
    )
    failing_player.status = "i"
    healthy_player.status = "i"
    db_session.commit()

    job = _run(db_session)

    assert job.output_json["alerts_sent"] == 1  # only the healthy player's alert succeeded
    assert _state_for(db_session, failing_player.id).status == "a"  # left for retry
    assert _state_for(db_session, healthy_player.id).status == "i"  # correctly advanced
