"""Integration tests for the /optimize/* endpoints against a real database.

Seeds data via ``execute_job`` on ``db_session``, exercises the HTTP layer via
``TestClient`` (which uses its own request-scoped session through
``apps.api.dependencies.get_db`` — both point at the same real Postgres).
"""

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.jobs.service import execute_job
from apps.api.main import create_app
from packages.database.models.ingestion_run import IngestionRun
from packages.database.models.manager_squad_snapshot import ManagerSquadSnapshot
from packages.database.models.player import Player
from packages.database.models.season import Season
from packages.domain.jobs import JobStatus, JobType, TriggerType
from packages.domain.transfers import ManagerSquadSnapshotSource


def _seed(db_session):
    ingestion_job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert ingestion_job.status == JobStatus.SUCCEEDED
    return ingestion_job


# Same 15-element squad as tests/integration/test_transfer_optimization_job.py's
# _create_manual_snapshot -- deliberately duplicated here rather than imported,
# since this module tests the HTTP layer specifically (request/response shape,
# the aliased Player joins in _load_transfer_run_response) and doesn't need the
# job-level module's hand-verified exact expected recommendation.
_SQUAD_ELEMENT_IDS = [
    30001,
    30002,
    30010,
    30011,
    30019,
    30014,
    30016,
    30028,
    30029,
    30030,
    30031,
    30032,
    30048,
    30049,
    30050,
]


def _seed_with_active_gameweek(db_session):
    ingestion_job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert ingestion_job.status == JobStatus.SUCCEEDED
    season_id = db_session.execute(
        select(Season.id).where(Season.label == ingestion_job.output_json["season_label"])
    ).scalar_one()
    return season_id


def _create_manual_snapshot(db_session, season_id) -> ManagerSquadSnapshot:
    player_by_element = {
        eid: db_session.execute(
            select(Player).where(Player.season_id == season_id, Player.fpl_element_id == eid)
        ).scalar_one()
        for eid in _SQUAD_ELEMENT_IDS
    }
    squad_player_ids = [str(player_by_element[eid].id) for eid in _SQUAD_ELEMENT_IDS]
    price_by_player = {
        str(player_by_element[eid].id): player_by_element[eid].now_cost for eid in _SQUAD_ELEMENT_IDS
    }

    snapshot = ManagerSquadSnapshot(
        season_id=season_id,
        source=ManagerSquadSnapshotSource.MANUAL_ENTRY,
        fpl_manager_id=None,
        job_id=None,
        as_of_gameweek_id=None,
        bank=20,
        free_transfers_available=2,
        active_chip=None,
        chips_used_json=None,
        overall_rank=None,
        total_points=None,
        squad_player_ids=squad_player_ids,
        squad_player_purchase_prices_json=price_by_player,
        squad_player_selling_prices_json=price_by_player,
        estimated_purchase_price_player_ids_json=[],
        unresolved_fpl_element_ids_json=[],
        has_unresolved_players=False,
        raw_entry_json=None,
        raw_history_json=None,
        raw_picks_json=None,
        raw_transfers_json=None,
        is_current=False,
        superseded_at=None,
    )
    db_session.add(snapshot)
    db_session.commit()
    db_session.refresh(snapshot)
    return snapshot


def test_optimize_initial_squad_happy_path(db_session, patched_fpl_client_optimization):
    _seed(db_session)
    client = TestClient(create_app())

    response = client.post("/optimize/initial-squad", json={"requested_by": "api-test"})

    assert response.status_code == 200
    body = response.json()
    assert body["recommendation_type"] == "joint_squad_and_xi"
    assert body["run_status"] == "succeeded"
    assert body["solver_status"] == "optimal"
    assert len(body["players"]) == 15
    assert body["squad_total_cost"] is not None
    assert body["squad_total_cost"] <= 1000
    assert body["squad_projected_points"] is not None

    # Fix 2: /optimize/initial-squad now returns a full XI in one response,
    # not just a squad list — starters/bench/captain/vice must all be present
    # and structurally correct.
    assert body["xi_projected_points"] is not None
    assert body["captain_player_id"] is not None
    assert body["vice_captain_player_id"] is not None
    assert body["captain_player_id"] != body["vice_captain_player_id"]

    starters = [p for p in body["players"] if p["is_starting"]]
    bench = [p for p in body["players"] if not p["is_starting"]]
    assert len(starters) == 11
    assert len(bench) == 4
    assert sorted(p["bench_order"] for p in bench) == [12, 13, 14, 15]
    assert all(p["bench_order"] is None for p in starters)

    captains = [p for p in starters if p["is_captain"]]
    vice_captains = [p for p in starters if p["is_vice_captain"]]
    assert len(captains) == 1
    assert len(vice_captains) == 1
    assert captains[0]["player_id"] == body["captain_player_id"]
    assert vice_captains[0]["player_id"] == body["vice_captain_player_id"]


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
    assert sorted(p["bench_order"] for p in bench) == [12, 13, 14, 15]
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


def test_optimize_transfers_happy_path(db_session, patched_fpl_client_optimization_with_active_gameweek):
    season_id = _seed_with_active_gameweek(db_session)
    snapshot = _create_manual_snapshot(db_session, season_id)
    client = TestClient(create_app())

    response = client.post(
        "/optimize/transfers",
        json={
            "manager_squad_snapshot_id": str(snapshot.id),
            "horizon_gameweeks": 6,
            "requested_by": "api-test",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["manager_squad_snapshot_id"] == str(snapshot.id)
    assert body["run_status"] == "succeeded"
    assert body["free_transfers_available"] == 2
    assert body["bank"] == 20
    assert body["recommended_action"] in {"roll", "one_transfer", "two_transfers"}

    assert len(body["scenarios"]) == 3
    transfers_counts = sorted(s["transfers_count"] for s in body["scenarios"])
    assert transfers_counts == [0, 1, 2]
    recommended = [s for s in body["scenarios"] if s["is_recommended"]]
    assert len(recommended) == 1

    zero_scenario = next(s for s in body["scenarios"] if s["transfers_count"] == 0)
    assert zero_scenario["moves"] == []
    for scenario in body["scenarios"]:
        assert len(scenario["moves"]) == scenario["transfers_count"]
        for move in scenario["moves"]:
            assert move["player_out_web_name"]
            assert move["player_in_web_name"]
            assert move["player_out_id"] != move["player_in_id"]


def test_optimize_transfers_refused_returns_409(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    season_id = _seed_with_active_gameweek(db_session)
    snapshot = _create_manual_snapshot(db_session, season_id)
    ingestion_run = (
        db_session.execute(select(IngestionRun).order_by(IngestionRun.completed_at.desc())).scalars().first()
    )
    ingestion_run.optimization_permitted = False
    db_session.commit()

    client = TestClient(create_app())
    response = client.post("/optimize/transfers", json={"manager_squad_snapshot_id": str(snapshot.id)})

    assert response.status_code == 409
    body = response.json()
    assert body["error_code"] == "OPTIMIZATION_NOT_PERMITTED"
    assert "job_id" in body


def test_optimize_transfers_unknown_snapshot_returns_409(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    _seed_with_active_gameweek(db_session)
    client = TestClient(create_app())

    response = client.post("/optimize/transfers", json={"manager_squad_snapshot_id": str(uuid.uuid4())})

    assert response.status_code == 409
    body = response.json()
    assert body["error_code"] == "SNAPSHOT_NOT_FOUND"
