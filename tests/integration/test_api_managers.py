"""Integration tests for the /managers/sync endpoint against a real database.

Mirrors every other ``test_api_*.py`` module's own "duplicate a minimal
setup rather than import from the job-level test module" discipline -- this
tests the HTTP layer specifically (request/response shape), not
MANAGER_SYNC's own sync logic, which is already covered in depth by
``test_manager_sync_job.py``.
"""

from app.jobs.service import execute_job
from packages.domain.jobs import JobStatus, JobType, TriggerType
from tests.integration.conftest import authed_client

FPL_MANAGER_ID = 1


def _seed_players(db_session):
    ingestion_job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert ingestion_job.status == JobStatus.SUCCEEDED


def test_sync_manager_happy_path_with_unresolved_player(
    db_session, patched_fpl_client_optimization, patched_fpl_client_manager
):
    _seed_players(db_session)
    client = authed_client()

    response = client.post("/managers/sync", json={"fpl_manager_id": FPL_MANAGER_ID})

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "manager_sync"
    assert body["fpl_manager_id"] == FPL_MANAGER_ID
    assert body["is_current"] is True
    assert body["has_unresolved_players"] is True
    assert body["unresolved_fpl_element_ids"] == [99999]
    assert len(body["players"]) == len(body["squad_player_ids"])


def test_sync_manager_rejects_missing_body_fields_with_422(db_session):
    client = authed_client()

    response = client.post("/managers/sync", json={})

    assert response.status_code == 422
