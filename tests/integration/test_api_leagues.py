"""Integration tests for the /leagues/{id}/rivals and /leagues/{id}/standings endpoints.

Reuses the same seeding/fixture setup as ``test_rival_profile.py``/
``test_league_sync_job.py`` -- this module tests the HTTP layer specifically
(request/response shape, the 404-when-league-unknown mapping), not
LEAGUE_SYNC's own rival-derivation logic, which is already covered in depth
by ``test_rival_profile.py``.
"""

import uuid

from sqlalchemy import select

from app.jobs.service import execute_job
from packages.database.models.league import League
from packages.database.models.season import Season
from packages.domain.jobs import JobStatus, JobType, TriggerType
from tests.integration.conftest import (
    LEAGUE_RIVAL_B_FPL_MANAGER_ID,
    LEAGUE_SELF_FPL_MANAGER_ID,
    authed_client,
)

FPL_LEAGUE_ID = 88894  # distinct from every other test module's own league id
FPL_LEAGUE_ID_2 = 88895  # a second, distinct id -- used only by the name-tiebreak test below


def _seed(db_session):
    ingestion_job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert ingestion_job.status == JobStatus.SUCCEEDED


def _sync(db_session, fpl_league_id: int):
    sync_job = execute_job(
        db_session,
        job_type=JobType.LEAGUE_SYNC,
        parameters={"fpl_league_id": fpl_league_id, "exclude_fpl_manager_id": LEAGUE_SELF_FPL_MANAGER_ID},
        requested_by="integration-test",
        trigger_type=TriggerType.LOCAL,
    )
    assert sync_job.status == JobStatus.SUCCEEDED


def _seed_and_sync(db_session, patched_fpl_client_optimization, patched_fpl_client_league) -> uuid.UUID:
    _seed(db_session)
    _sync(db_session, FPL_LEAGUE_ID)
    season_id = db_session.execute(select(Season.id).where(Season.is_current.is_(True))).scalar_one()
    return season_id


def _league_id(db_session, fpl_league_id: int = FPL_LEAGUE_ID) -> uuid.UUID:
    return db_session.execute(select(League.id).where(League.fpl_league_id == fpl_league_id)).scalar_one()


def test_list_leagues_includes_every_synced_league(
    db_session, patched_fpl_client_optimization, patched_fpl_client_league
):
    _seed_and_sync(db_session, patched_fpl_client_optimization, patched_fpl_client_league)
    league_id = _league_id(db_session)
    client = authed_client()

    response = client.get("/leagues")

    assert response.status_code == 200
    body = response.json()
    # Other test modules sync their OWN distinct fpl_league_id against this
    # same non-truncated Postgres (League is upsert-in-place, accumulates
    # forever across every historical run) -- assert presence, not an exact
    # count.
    matching = [row for row in body if row["league_id"] == str(league_id)]
    assert len(matching) == 1
    assert matching[0]["fpl_league_id"] == FPL_LEAGUE_ID


def test_list_leagues_breaks_identical_name_ties_by_fpl_league_id(
    db_session, patched_fpl_client_optimization, patched_fpl_client_league
):
    # patched_fpl_client_league's fixture names every synced league "Test H2H
    # League" regardless of which fpl_league_id was requested -- two distinct
    # League rows sharing that exact name is the realistic way this ordering
    # tiebreak actually gets exercised (see list_leagues's own docstring: an
    # unordered/single-key-with-ties query has no obligation to return a
    # stable order across executions).
    _seed(db_session)
    _sync(db_session, FPL_LEAGUE_ID_2)
    _sync(db_session, FPL_LEAGUE_ID)
    client = authed_client()

    response = client.get("/leagues")

    assert response.status_code == 200
    body = response.json()
    same_named = [row for row in body if row["fpl_league_id"] in {FPL_LEAGUE_ID, FPL_LEAGUE_ID_2}]
    assert [row["fpl_league_id"] for row in same_named] == [FPL_LEAGUE_ID, FPL_LEAGUE_ID_2]


def test_list_league_rivals_returns_every_synced_rival(
    db_session, patched_fpl_client_optimization, patched_fpl_client_league
):
    _seed_and_sync(db_session, patched_fpl_client_optimization, patched_fpl_client_league)
    league_id = _league_id(db_session)
    client = authed_client()

    response = client.get(f"/leagues/{league_id}/rivals")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 3  # rivals B, C, D (self excluded by default LEAGUE_SYNC parameters)
    manager_ids = {row["fpl_manager_id"] for row in body}
    assert LEAGUE_RIVAL_B_FPL_MANAGER_ID in manager_ids
    for row in body:
        assert row["overall_confidence"] in {"low", "medium", "high"}
        assert isinstance(row["derived_likely_remaining_chips"], list)


def test_list_league_rivals_unknown_league_returns_404():
    client = authed_client()

    response = client.get(f"/leagues/{uuid.uuid4()}/rivals")

    assert response.status_code == 404
    assert response.json()["error_code"] == "LEAGUE_NOT_FOUND"


def test_list_league_standings_returns_every_entry(
    db_session, patched_fpl_client_optimization, patched_fpl_client_league
):
    _seed_and_sync(db_session, patched_fpl_client_optimization, patched_fpl_client_league)
    league_id = _league_id(db_session)
    client = authed_client()

    response = client.get(f"/leagues/{league_id}/standings")

    assert response.status_code == 200
    body = response.json()
    assert len(body) >= 3
    assert all("fpl_manager_id" in row for row in body)


def test_list_league_standings_unknown_league_returns_404():
    client = authed_client()

    response = client.get(f"/leagues/{uuid.uuid4()}/standings")

    assert response.status_code == 404
    assert response.json()["error_code"] == "LEAGUE_NOT_FOUND"
