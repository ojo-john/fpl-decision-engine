"""Integration tests for the /optimize/chips endpoint against a real database.

Mirrors ``test_api_h2h_simulation.py``'s own "duplicate a minimal setup rather
than import from the job-level test module" discipline -- this module tests
the HTTP layer specifically (request/response shape, error-to-409 mapping),
not the job's own chip-evaluation logic, which is already covered in depth by
``test_chip_simulation_job.py``.
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
_STARTING_ELEMENT_IDS = [30001, 30010, 30011, 30019, 30014, 30028, 30029, 30030, 30031, 30048, 30049]
_BENCH_ELEMENT_IDS = [30002, 30016, 30032, 30050]
_CAPTAIN_ELEMENT_ID = 30048
_VICE_CAPTAIN_ELEMENT_ID = 30049


def _raw_picks_json() -> dict:
    picks = []
    for position, element in enumerate(_STARTING_ELEMENT_IDS, start=1):
        picks.append(
            {
                "element": element,
                "position": position,
                "multiplier": 2 if element == _CAPTAIN_ELEMENT_ID else 1,
                "is_captain": element == _CAPTAIN_ELEMENT_ID,
                "is_vice_captain": element == _VICE_CAPTAIN_ELEMENT_ID,
            }
        )
    for offset, element in enumerate(_BENCH_ELEMENT_IDS):
        picks.append(
            {
                "element": element,
                "position": 12 + offset,
                "multiplier": 0,
                "is_captain": False,
                "is_vice_captain": False,
            }
        )
    return {
        "active_chip": None,
        "automatic_subs": [],
        "entry_history": {"event": 1, "bank": 5},
        "picks": picks,
    }


def _seed(db_session):
    ingestion_job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert ingestion_job.status == JobStatus.SUCCEEDED
    season_id = db_session.execute(
        select(Season.id).where(Season.label == ingestion_job.output_json["season_label"])
    ).scalar_one()
    return season_id


def _create_manual_snapshot(db_session, season_id, raw_picks_json: dict | None) -> ManagerSquadSnapshot:
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
        raw_picks_json=raw_picks_json,
        raw_transfers_json=None,
        is_current=False,
        superseded_at=None,
    )
    db_session.add(snapshot)
    db_session.commit()
    db_session.refresh(snapshot)
    return snapshot


def test_chip_optimization_happy_path(db_session, patched_fpl_client_optimization_with_active_gameweek):
    season_id = _seed(db_session)
    snapshot = _create_manual_snapshot(db_session, season_id, _raw_picks_json())
    client = authed_client()

    response = client.post("/optimize/chips", json={"manager_squad_snapshot_id": str(snapshot.id)})

    assert response.status_code == 200
    body = response.json()
    assert body["run_status"] == "succeeded"
    assert body["target_fpl_event_id"] == 38
    assert len(body["recommendations"]) == 4
    for recommendation in body["recommendations"]:
        assert recommendation["chip_type"] in {"wildcard", "freehit", "bboost", "3xc"}
        assert recommendation["verdict"] in {"use_now", "hold"}
        assert recommendation["rationale"]
        assert len(recommendation["candidates"]) == 1


def test_chip_optimization_respects_chip_types_filter(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    season_id = _seed(db_session)
    snapshot = _create_manual_snapshot(db_session, season_id, _raw_picks_json())
    client = authed_client()

    response = client.post(
        "/optimize/chips",
        json={"manager_squad_snapshot_id": str(snapshot.id), "chip_types": ["bboost"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["recommendations"]) == 1
    assert body["recommendations"][0]["chip_type"] == "bboost"


def test_chip_optimization_missing_snapshot_returns_409(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    _seed(db_session)
    client = authed_client()

    response = client.post("/optimize/chips", json={"manager_squad_snapshot_id": str(uuid.uuid4())})

    assert response.status_code == 409
    body = response.json()
    assert body["error_code"] == "SNAPSHOT_NOT_FOUND"
    assert "job_id" in body


def test_chip_optimization_rejects_missing_body_fields_with_422(db_session):
    client = authed_client()

    response = client.post("/optimize/chips", json={})

    assert response.status_code == 422


# --- GET /optimize/chips/{id} (Milestone 8 Layer 1) -------------------------------------------


def test_get_chip_optimization_run_returns_the_same_body_the_post_returned(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    season_id = _seed(db_session)
    snapshot = _create_manual_snapshot(db_session, season_id, _raw_picks_json())
    client = authed_client()
    created = client.post("/optimize/chips", json={"manager_squad_snapshot_id": str(snapshot.id)}).json()

    response = client.get(f"/optimize/chips/{created['chip_optimization_run_id']}")

    assert response.status_code == 200
    assert response.json() == created


def test_get_chip_optimization_run_unknown_id_returns_404():
    client = authed_client()

    response = client.get(f"/optimize/chips/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["error_code"] == "CHIP_OPTIMIZATION_RUN_NOT_FOUND"


# --- GET /optimize/chips (list/history, Milestone 8 item 1) ---------------------------------


def test_list_chip_optimization_runs_filters_by_snapshot(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    season_id = _seed(db_session)
    snapshot = _create_manual_snapshot(db_session, season_id, _raw_picks_json())
    client = authed_client()
    created = client.post("/optimize/chips", json={"manager_squad_snapshot_id": str(snapshot.id)}).json()

    response = client.get("/optimize/chips", params={"manager_squad_snapshot_id": str(snapshot.id)})

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["chip_optimization_run_id"] == created["chip_optimization_run_id"]
    assert body[0]["target_fpl_event_id"] == created["target_fpl_event_id"]
    assert "recommendations" not in body[0]


def test_list_chip_optimization_runs_empty_for_unknown_snapshot():
    client = authed_client()

    response = client.get("/optimize/chips", params={"manager_squad_snapshot_id": str(uuid.uuid4())})

    assert response.status_code == 200
    assert response.json() == []
