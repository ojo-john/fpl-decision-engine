"""Integration tests for POST /squads/manual against a real database.

Seeds current-season players via DATA_INGESTION (the ~60-player optimization
pool fixture, element ids 30000-30059), then exercises the manual-squad
endpoint's happy path and its three validation-rejection cases. Unlike
MANAGER_SYNC, an unresolvable id here is a 422, not a tolerated case -- see
``apps/api/schemas/managers.py``/``apps/api/routers/managers.py`` for why
that asymmetry is deliberate.
"""

from fastapi.testclient import TestClient

from app.jobs.service import execute_job
from apps.api.main import create_app
from packages.domain.jobs import JobStatus, JobType, TriggerType

_VALID_ELEMENT_IDS = [30000 + i for i in range(15)]


def _seed_players(db_session):
    ingestion_job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert ingestion_job.status == JobStatus.SUCCEEDED
    return ingestion_job


def test_manual_squad_happy_path(db_session, patched_fpl_client_optimization):
    _seed_players(db_session)
    client = TestClient(create_app())

    response = client.post(
        "/squads/manual",
        json={
            "fpl_element_ids": _VALID_ELEMENT_IDS,
            "bank": 5,
            "free_transfers_available": 1,
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["source"] == "manual_entry"
    assert body["bank"] == 5
    assert body["free_transfers_available"] == 1
    assert len(body["squad_player_ids"]) == 15
    assert body["has_unresolved_players"] is False
    assert body["unresolved_fpl_element_ids"] == []


def test_manual_squad_rejects_duplicate_ids_with_422(db_session, patched_fpl_client_optimization):
    _seed_players(db_session)
    client = TestClient(create_app())

    duplicated = _VALID_ELEMENT_IDS[:14] + [_VALID_ELEMENT_IDS[0]]
    response = client.post(
        "/squads/manual",
        json={"fpl_element_ids": duplicated, "bank": 0, "free_transfers_available": 1},
    )

    assert response.status_code == 422


def test_manual_squad_rejects_wrong_count_with_422(db_session, patched_fpl_client_optimization):
    _seed_players(db_session)
    client = TestClient(create_app())

    response = client.post(
        "/squads/manual",
        json={"fpl_element_ids": _VALID_ELEMENT_IDS[:14], "bank": 0, "free_transfers_available": 1},
    )

    assert response.status_code == 422


def test_manual_squad_rejects_unresolvable_id_with_422(db_session, patched_fpl_client_optimization):
    _seed_players(db_session)
    client = TestClient(create_app())

    unresolvable = _VALID_ELEMENT_IDS[:14] + [999999]
    response = client.post(
        "/squads/manual",
        json={"fpl_element_ids": unresolvable, "bank": 0, "free_transfers_available": 1},
    )

    assert response.status_code == 422
