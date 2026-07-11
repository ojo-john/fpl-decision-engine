"""Integration tests for the /optimize/* endpoints against a real database.

Seeds data via ``execute_job`` on ``db_session``, exercises the HTTP layer via
``TestClient`` (which uses its own request-scoped session through
``apps.api.dependencies.get_db`` — both point at the same real Postgres).
"""

import uuid

from fastapi.testclient import TestClient

from app.jobs.service import execute_job
from apps.api.main import create_app
from packages.database.models.ingestion_run import IngestionRun
from packages.domain.jobs import JobStatus, JobType, TriggerType


def _seed(db_session):
    ingestion_job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert ingestion_job.status == JobStatus.SUCCEEDED
    return ingestion_job


def test_optimize_initial_squad_happy_path(db_session, patched_fpl_client_optimization):
    _seed(db_session)
    client = TestClient(create_app())

    response = client.post("/optimize/initial-squad", json={"requested_by": "api-test"})

    assert response.status_code == 200
    body = response.json()
    assert body["recommendation_type"] == "initial_squad"
    assert body["run_status"] == "succeeded"
    assert body["solver_status"] == "optimal"
    assert len(body["players"]) == 15
    assert body["squad_total_cost"] is not None
    assert body["squad_total_cost"] <= 1000
    assert body["squad_projected_points"] is not None


def test_optimize_initial_squad_refused_returns_409(db_session, patched_fpl_client_optimization):
    ingestion_job = _seed(db_session)
    ingestion_run_id = uuid.UUID(ingestion_job.output_json["ingestion_run_id"])
    ingestion_run = db_session.get(IngestionRun, ingestion_run_id)
    ingestion_run.optimization_permitted = False
    db_session.commit()

    client = TestClient(create_app())
    response = client.post("/optimize/initial-squad", json={})

    assert response.status_code == 409
    body = response.json()
    assert body["error_code"] == "OPTIMIZATION_NOT_PERMITTED"
    assert "job_id" in body


def test_optimize_lineup_chained_from_squad_run(db_session, patched_fpl_client_optimization):
    _seed(db_session)
    client = TestClient(create_app())

    squad_response = client.post("/optimize/initial-squad", json={})
    assert squad_response.status_code == 200
    squad_run_id = squad_response.json()["optimization_run_id"]

    response = client.post("/optimize/lineup", json={"source_optimization_run_id": squad_run_id})

    assert response.status_code == 200
    body = response.json()
    assert body["recommendation_type"] == "starting_xi"
    assert body["run_status"] == "succeeded"
    starters = [p for p in body["players"] if p["is_starting"]]
    bench = [p for p in body["players"] if not p["is_starting"]]
    assert len(starters) == 11
    assert len(bench) == 4
    assert sorted(p["bench_order"] for p in bench) == [1, 2, 3, 4]
    captains = [p for p in starters if p["is_captain"]]
    vice_captains = [p for p in starters if p["is_vice_captain"]]
    assert len(captains) == 1
    assert len(vice_captains) == 1
    assert captains[0]["player_id"] != vice_captains[0]["player_id"]
    assert body["xi_projected_points"] is not None


def test_optimize_lineup_explicit_player_ids(db_session, patched_fpl_client_optimization):
    _seed(db_session)
    client = TestClient(create_app())
    squad_run = client.post("/optimize/initial-squad", json={}).json()
    player_ids = [p["player_id"] for p in squad_run["players"]]

    response = client.post("/optimize/lineup", json={"player_ids": player_ids})

    assert response.status_code == 200
    body = response.json()
    assert len([p for p in body["players"] if p["is_starting"]]) == 11


def test_optimize_lineup_rejects_ambiguous_input_with_422(db_session, patched_fpl_client_optimization):
    _seed(db_session)
    client = TestClient(create_app())

    response = client.post(
        "/optimize/lineup",
        json={"source_optimization_run_id": str(uuid.uuid4()), "player_ids": [str(uuid.uuid4())]},
    )

    assert response.status_code == 422


def test_optimize_lineup_rejects_missing_input_with_422(db_session, patched_fpl_client_optimization):
    _seed(db_session)
    client = TestClient(create_app())

    response = client.post("/optimize/lineup", json={})

    assert response.status_code == 422
