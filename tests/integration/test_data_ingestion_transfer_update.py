"""Integration test: a player transferred to a different team updates the same row in place.

Requires a running Postgres with migrations applied (see conftest.py).
"""

from sqlalchemy import select

from app.jobs.service import execute_job
from packages.database.models.player import Player
from packages.database.models.season import Season
from packages.database.models.team import Team
from packages.domain.jobs import JobStatus, JobType, TriggerType


def _find_player_element(bootstrap_json, fpl_code):
    return next(e for e in bootstrap_json["elements"] if e["code"] == fpl_code)


def test_player_team_change_updates_same_row(db_session, patched_fpl_client):
    first = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert first.status == JobStatus.SUCCEEDED
    season_label = first.output_json["season_label"]
    season_id = db_session.execute(select(Season.id).where(Season.label == season_label)).scalar_one()

    player_before = db_session.execute(
        select(Player).where(Player.season_id == season_id, Player.fpl_code == 900101)
    ).scalar_one()
    original_team_id = player_before.team_id
    player_row_id = player_before.id

    # Move TestGKP (code 900101) from its original team to a different, still-valid team.
    original_element = _find_player_element(patched_fpl_client["bootstrap"], 900101)
    new_fpl_team_id = 20 if original_element["team"] != 20 else 19
    original_element["team"] = new_fpl_team_id

    second = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert second.status == JobStatus.SUCCEEDED

    player_after = db_session.execute(
        select(Player).where(Player.season_id == season_id, Player.fpl_code == 900101)
    ).scalar_one()

    assert player_after.id == player_row_id  # same row, not a new one
    assert player_after.team_id != original_team_id

    new_team = db_session.get(Team, player_after.team_id)
    assert new_team.fpl_team_id == new_fpl_team_id

    all_matching = (
        db_session.execute(select(Player).where(Player.season_id == season_id, Player.fpl_code == 900101))
        .scalars()
        .all()
    )
    assert len(all_matching) == 1
