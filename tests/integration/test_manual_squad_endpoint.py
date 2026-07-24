"""Integration tests for POST /squads/manual and the GET /squads* endpoints against a real
database.

Seeds current-season players via DATA_INGESTION (the ~60-player optimization
pool fixture, element ids 30000-30059), then exercises the manual-squad
endpoint's happy path and its three validation-rejection cases. Unlike
MANAGER_SYNC, an unresolvable id here is a 422, not a tolerated case -- see
``apps/api/schemas/managers.py``/``apps/api/routers/managers.py`` for why
that asymmetry is deliberate.
"""

import uuid

from app.jobs.service import execute_job
from packages.domain.jobs import JobStatus, JobType, TriggerType
from tests.integration.conftest import authed_client

_VALID_ELEMENT_IDS = [30000 + i for i in range(15)]


def _seed_players(db_session):
    ingestion_job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert ingestion_job.status == JobStatus.SUCCEEDED
    return ingestion_job


def test_manual_squad_happy_path(db_session, patched_fpl_client_optimization):
    _seed_players(db_session)
    client = authed_client()

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
    assert body["fpl_manager_id"] is None
    assert body["is_current"] is False
    assert body["bank"] == 5
    assert body["free_transfers_available"] == 1
    assert len(body["squad_player_ids"]) == 15
    assert len(body["players"]) == 15
    for player in body["players"]:
        assert player["web_name"]
        assert player["position"] in {"GKP", "DEF", "MID", "FWD"}
        assert player["now_cost"] > 0
    assert body["has_unresolved_players"] is False
    assert body["unresolved_fpl_element_ids"] == []
    # Manual entries never have raw_picks_json -- see ManagerSquadSnapshot's own docstring.
    assert body["has_raw_picks"] is False


def test_manual_squad_rejects_duplicate_ids_with_422(db_session, patched_fpl_client_optimization):
    _seed_players(db_session)
    client = authed_client()

    duplicated = _VALID_ELEMENT_IDS[:14] + [_VALID_ELEMENT_IDS[0]]
    response = client.post(
        "/squads/manual",
        json={"fpl_element_ids": duplicated, "bank": 0, "free_transfers_available": 1},
    )

    assert response.status_code == 422


def test_manual_squad_rejects_wrong_count_with_422(db_session, patched_fpl_client_optimization):
    _seed_players(db_session)
    client = authed_client()

    response = client.post(
        "/squads/manual",
        json={"fpl_element_ids": _VALID_ELEMENT_IDS[:14], "bank": 0, "free_transfers_available": 1},
    )

    assert response.status_code == 422


def test_manual_squad_rejects_unresolvable_id_with_422(db_session, patched_fpl_client_optimization):
    _seed_players(db_session)
    client = authed_client()

    unresolvable = _VALID_ELEMENT_IDS[:14] + [999999]
    response = client.post(
        "/squads/manual",
        json={"fpl_element_ids": unresolvable, "bank": 0, "free_transfers_available": 1},
    )

    assert response.status_code == 422


# --- GET /squads/{id}, GET /squads (Milestone 8) -----------------------------------------------


def test_get_squad_snapshot_returns_the_same_body_the_post_returned(
    db_session, patched_fpl_client_optimization
):
    _seed_players(db_session)
    client = authed_client()
    created = client.post(
        "/squads/manual",
        json={"fpl_element_ids": _VALID_ELEMENT_IDS, "bank": 5, "free_transfers_available": 1},
    ).json()

    response = client.get(f"/squads/{created['manager_squad_snapshot_id']}")

    assert response.status_code == 200
    assert response.json() == created


def test_get_squad_snapshot_unknown_id_returns_404():
    client = authed_client()

    response = client.get(f"/squads/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["error_code"] == "MANAGER_SQUAD_SNAPSHOT_NOT_FOUND"


def test_list_squad_snapshots_includes_a_newly_created_manual_entry(
    db_session, patched_fpl_client_optimization
):
    _seed_players(db_session)
    client = authed_client()
    created = client.post(
        "/squads/manual",
        json={"fpl_element_ids": _VALID_ELEMENT_IDS, "bank": 5, "free_transfers_available": 1},
    ).json()

    response = client.get("/squads")

    assert response.status_code == 200
    ids = {row["manager_squad_snapshot_id"] for row in response.json()}
    assert created["manager_squad_snapshot_id"] in ids


def test_delete_manual_squad_snapshot_happy_path(db_session, patched_fpl_client_optimization):
    _seed_players(db_session)
    client = authed_client()
    created = client.post(
        "/squads/manual",
        json={"fpl_element_ids": _VALID_ELEMENT_IDS, "bank": 5, "free_transfers_available": 1},
    ).json()
    snapshot_id = created["manager_squad_snapshot_id"]

    response = client.delete(f"/squads/{snapshot_id}")

    assert response.status_code == 204
    assert client.get(f"/squads/{snapshot_id}").status_code == 404


def test_delete_manual_squad_snapshot_unknown_id_returns_404():
    client = authed_client()

    response = client.delete(f"/squads/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["error_code"] == "MANAGER_SQUAD_SNAPSHOT_NOT_FOUND"


def test_delete_manager_sync_snapshot_returns_409(
    db_session, patched_fpl_client_optimization, patched_fpl_client_manager
):
    _seed_players(db_session)
    client = authed_client()
    sync_response = client.post("/managers/sync", json={"fpl_manager_id": 1})
    assert sync_response.status_code == 200, sync_response.json()
    snapshot_id = sync_response.json()["manager_squad_snapshot_id"]

    response = client.delete(f"/squads/{snapshot_id}")

    assert response.status_code == 409
    assert response.json()["error_code"] == "SNAPSHOT_NOT_DELETABLE"


def test_delete_manual_squad_with_dependent_run_returns_409(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    _seed_players(db_session)
    client = authed_client()
    created = client.post(
        "/squads/manual",
        json={"fpl_element_ids": _VALID_ELEMENT_IDS, "bank": 5, "free_transfers_available": 1},
    ).json()
    snapshot_id = created["manager_squad_snapshot_id"]
    transfer_response = client.post("/optimize/transfers", json={"manager_squad_snapshot_id": snapshot_id})
    assert transfer_response.status_code == 200, transfer_response.json()

    response = client.delete(f"/squads/{snapshot_id}")

    assert response.status_code == 409
    assert response.json()["error_code"] == "SNAPSHOT_HAS_DEPENDENT_RUNS"
    assert client.get(f"/squads/{snapshot_id}").status_code == 200  # not actually deleted


def test_list_squad_snapshots_filters_by_is_current(db_session, patched_fpl_client_optimization):
    _seed_players(db_session)
    client = authed_client()
    created = client.post(
        "/squads/manual",
        json={"fpl_element_ids": _VALID_ELEMENT_IDS, "bank": 5, "free_transfers_available": 1},
    ).json()

    response = client.get("/squads", params={"is_current": "true"})

    assert response.status_code == 200
    # Manual entries never set is_current=True (see ManagerSquadSnapshot's own
    # docstring) -- this manual row must never show up in an is_current=True filter.
    ids = {row["manager_squad_snapshot_id"] for row in response.json()}
    assert created["manager_squad_snapshot_id"] not in ids
