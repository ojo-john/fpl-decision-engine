"""Integration tests for the /recommendations endpoints against a real database.

Mirrors ``test_api_chip_optimization.py``'s own "duplicate a minimal setup
rather than import from the job-level test module" discipline -- this module
tests the HTTP layer specifically (request/response shape, error-to-409/404
mapping), not the job's own correlation logic, which is already covered in
depth by ``test_recommendation_generation_job.py``.
"""

import uuid

from sqlalchemy import select

from app.jobs.service import execute_job
from packages.database.models.manager_squad_snapshot import ManagerSquadSnapshot
from packages.database.models.player import Player
from packages.database.models.season import Season
from packages.domain.jobs import JobStatus, JobType, TriggerType
from packages.domain.transfers import ManagerSquadSnapshotSource
from tests.integration.conftest import authed_client

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


def _seed(db_session):
    ingestion_job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert ingestion_job.status == JobStatus.SUCCEEDED
    season_id = db_session.execute(
        select(Season.id).where(Season.label == ingestion_job.output_json["season_label"])
    ).scalar_one()
    return season_id


def _create_manual_snapshot(db_session, season_id) -> ManagerSquadSnapshot:
    players = {
        eid: db_session.execute(
            select(Player).where(Player.season_id == season_id, Player.fpl_element_id == eid)
        ).scalar_one()
        for eid in _SQUAD_ELEMENT_IDS
    }
    squad_player_ids = [str(players[eid].id) for eid in _SQUAD_ELEMENT_IDS]
    price_by_player = {str(players[eid].id): players[eid].now_cost for eid in _SQUAD_ELEMENT_IDS}

    snapshot = ManagerSquadSnapshot(
        season_id=season_id,
        source=ManagerSquadSnapshotSource.MANUAL_ENTRY,
        fpl_manager_id=None,
        job_id=None,
        as_of_gameweek_id=None,
        bank=20,
        free_transfers_available=1,
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


def test_recommendation_generate_happy_path(db_session, patched_fpl_client_optimization_with_active_gameweek):
    season_id = _seed(db_session)
    snapshot = _create_manual_snapshot(db_session, season_id)
    client = authed_client()

    response = client.post("/recommendations/generate", json={"manager_squad_snapshot_id": str(snapshot.id)})

    assert response.status_code == 200
    body = response.json()
    assert body["target_fpl_event_id"] == 38
    assert body["is_authoritative"] is True
    assert body["confidence"] in {"high", "medium", "low"}
    assert body["optimization"] is None  # SQUAD_OPTIMIZATION was never run in this test
    assert body["transfer_optimization"] is None
    assert body["h2h_simulation"] is None
    assert body["chip_optimization"] is None
    assert isinstance(body["known_risks"], list)
    assert isinstance(body["assumptions"], list)


def test_recommendation_generate_off_season_returns_409(db_session, patched_fpl_client_optimization):
    _seed(db_session)
    client = authed_client()

    response = client.post("/recommendations/generate", json={})

    assert response.status_code == 409
    body = response.json()
    assert body["error_code"] == "NO_ACTIVE_GAMEWEEK"
    assert "job_id" in body


def test_recommendation_generate_missing_snapshot_returns_409(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    _seed(db_session)
    client = authed_client()

    response = client.post("/recommendations/generate", json={"manager_squad_snapshot_id": str(uuid.uuid4())})

    assert response.status_code == 409
    body = response.json()
    assert body["error_code"] == "SNAPSHOT_NOT_FOUND"


def test_recommendation_get_by_id_happy_path(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    season_id = _seed(db_session)
    snapshot = _create_manual_snapshot(db_session, season_id)
    client = authed_client()

    generate_response = client.post(
        "/recommendations/generate", json={"manager_squad_snapshot_id": str(snapshot.id)}
    )
    assert generate_response.status_code == 200
    recommendation_audit_id = generate_response.json()["recommendation_audit_id"]

    get_response = client.get(f"/recommendations/{recommendation_audit_id}")

    assert get_response.status_code == 200
    assert get_response.json()["recommendation_audit_id"] == recommendation_audit_id


def test_recommendation_get_by_id_nonexistent_returns_404(db_session):
    client = authed_client()

    response = client.get(f"/recommendations/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["error_code"] == "RECOMMENDATION_AUDIT_NOT_FOUND"


def test_recommendation_get_latest_happy_path(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    season_id = _seed(db_session)
    snapshot = _create_manual_snapshot(db_session, season_id)
    client = authed_client()

    generate_response = client.post(
        "/recommendations/generate", json={"manager_squad_snapshot_id": str(snapshot.id)}
    )
    assert generate_response.status_code == 200
    recommendation_audit_id = generate_response.json()["recommendation_audit_id"]

    latest_response = client.get("/recommendations/latest")

    assert latest_response.status_code == 200
    assert latest_response.json()["recommendation_audit_id"] == recommendation_audit_id


def test_recommendation_get_latest_none_exists_returns_404(db_session, patched_fpl_client_optimization):
    _seed(db_session)
    client = authed_client()

    response = client.get("/recommendations/latest")

    assert response.status_code == 404
    assert response.json()["error_code"] == "RECOMMENDATION_AUDIT_NOT_FOUND"
