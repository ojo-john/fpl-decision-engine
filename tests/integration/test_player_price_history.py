"""Integration tests for player_price_history: DATA_INGESTION only records a row when a
player's now_cost actually changes between two runs, never on every run.

Mirrors test_data_ingestion_transfer_update.py's own "mutate the mocked bootstrap payload
between two execute_job calls" discipline -- the same real, end-to-end path a live price
change would go through.
"""

from sqlalchemy import select

from app.jobs.service import execute_job
from packages.database.models.player import Player
from packages.database.models.player_price_history import PlayerPriceHistory
from packages.database.models.season import Season
from packages.domain.jobs import JobStatus, JobType, TriggerType


def _find_player_element(bootstrap_json, fpl_code):
    return next(e for e in bootstrap_json["elements"] if e["code"] == fpl_code)


def _price_history_for(db_session, player_id):
    return (
        db_session.execute(
            select(PlayerPriceHistory)
            .where(PlayerPriceHistory.player_id == player_id)
            .order_by(PlayerPriceHistory.observed_at)
        )
        .scalars()
        .all()
    )


def test_identical_rerun_creates_no_price_history_row(db_session, patched_fpl_client):
    first = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert first.status == JobStatus.SUCCEEDED
    season_id = db_session.execute(
        select(Season.id).where(Season.label == first.output_json["season_label"])
    ).scalar_one()
    player = db_session.execute(
        select(Player).where(Player.season_id == season_id, Player.fpl_code == 900101)
    ).scalar_one()

    second = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert second.status == JobStatus.SUCCEEDED

    assert _price_history_for(db_session, player.id) == []


def test_real_price_change_creates_exactly_one_row(db_session, patched_fpl_client):
    first = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert first.status == JobStatus.SUCCEEDED
    season_id = db_session.execute(
        select(Season.id).where(Season.label == first.output_json["season_label"])
    ).scalar_one()
    player_before = db_session.execute(
        select(Player).where(Player.season_id == season_id, Player.fpl_code == 900101)
    ).scalar_one()
    original_cost = player_before.now_cost
    player_row_id = player_before.id

    element = _find_player_element(patched_fpl_client["bootstrap"], 900101)
    element["now_cost"] = original_cost + 1  # a real +£0.1m rise

    second = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert second.status == JobStatus.SUCCEEDED

    player_after = db_session.execute(
        select(Player).where(Player.season_id == season_id, Player.fpl_code == 900101)
    ).scalar_one()
    assert player_after.id == player_row_id  # same row, not a new one
    assert player_after.now_cost == original_cost + 1

    history = _price_history_for(db_session, player_row_id)
    assert len(history) == 1
    assert history[0].old_cost == original_cost
    assert history[0].new_cost == original_cost + 1
    assert history[0].season_id == season_id


def test_two_real_changes_across_three_runs_creates_two_rows_in_order(
    db_session, patched_fpl_client
):
    first = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert first.status == JobStatus.SUCCEEDED
    season_id = db_session.execute(
        select(Season.id).where(Season.label == first.output_json["season_label"])
    ).scalar_one()
    player = db_session.execute(
        select(Player).where(Player.season_id == season_id, Player.fpl_code == 900101)
    ).scalar_one()
    cost_0 = player.now_cost

    element = _find_player_element(patched_fpl_client["bootstrap"], 900101)
    element["now_cost"] = cost_0 + 1
    second = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert second.status == JobStatus.SUCCEEDED

    element["now_cost"] = cost_0  # a real fall back to the original price
    third = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert third.status == JobStatus.SUCCEEDED

    history = _price_history_for(db_session, player.id)
    assert len(history) == 2
    assert (history[0].old_cost, history[0].new_cost) == (cost_0, cost_0 + 1)
    assert (history[1].old_cost, history[1].new_cost) == (cost_0 + 1, cost_0)
