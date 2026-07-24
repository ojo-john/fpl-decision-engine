"""Integration tests for the /h2h/simulate endpoint against a real database.

Mirrors ``test_api_optimization.py``'s own "duplicate a minimal setup rather
than import from the job-level test module" discipline (see that file's own
docstring) -- this module tests the HTTP layer specifically (request/response
shape, error-to-409 mapping), not the job's own simulation logic, which is
already covered in depth by ``test_h2h_simulation_job.py``.
"""

import uuid

import pytest
from sqlalchemy import select

from app.jobs.service import execute_job
from packages.database.models.manager_squad_snapshot import ManagerSquadSnapshot
from packages.database.models.player import Player
from packages.database.models.rival_profile import RivalProfile
from packages.database.models.season import Season
from packages.domain.jobs import JobStatus, JobType, TriggerType
from packages.domain.transfers import ManagerSquadSnapshotSource
from tests.integration.conftest import (
    LEAGUE_RIVAL_B_FPL_MANAGER_ID,
    LEAGUE_SELF_FPL_MANAGER_ID,
    authed_client,
)

FPL_LEAGUE_ID = 88892  # distinct from every other test file's league id

_STARTING_ELEMENTS = [30003, 30015, 30016, 30018, 30033, 30035, 30036, 30037, 30052, 30053, 30054]
_CAPTAIN_ELEMENT = 30033
_VICE_CAPTAIN_ELEMENT = 30035
_BENCH_ELEMENTS = [30004, 30019, 30038, 30056]


def _raw_picks_json() -> dict:
    picks = []
    for position, element in enumerate(_STARTING_ELEMENTS, start=1):
        picks.append(
            {
                "element": element,
                "position": position,
                "multiplier": 2 if element == _CAPTAIN_ELEMENT else 1,
                "is_captain": element == _CAPTAIN_ELEMENT,
                "is_vice_captain": element == _VICE_CAPTAIN_ELEMENT,
            }
        )
    for offset, element in enumerate(_BENCH_ELEMENTS):
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


def _run_league_sync(db_session):
    return execute_job(
        db_session,
        job_type=JobType.LEAGUE_SYNC,
        parameters={"fpl_league_id": FPL_LEAGUE_ID, "exclude_fpl_manager_id": LEAGUE_SELF_FPL_MANAGER_ID},
        requested_by="integration-test",
        trigger_type=TriggerType.LOCAL,
    )


def _create_manual_snapshot(db_session, season_id) -> ManagerSquadSnapshot:
    all_elements = _STARTING_ELEMENTS + _BENCH_ELEMENTS
    players = {
        eid: db_session.execute(
            select(Player).where(Player.season_id == season_id, Player.fpl_element_id == eid)
        ).scalar_one()
        for eid in all_elements
    }
    squad_player_ids = [str(players[eid].id) for eid in all_elements]
    price_by_player = {str(players[eid].id): players[eid].now_cost for eid in all_elements}

    snapshot = ManagerSquadSnapshot(
        season_id=season_id,
        source=ManagerSquadSnapshotSource.MANUAL_ENTRY,
        fpl_manager_id=None,
        job_id=None,
        as_of_gameweek_id=None,
        bank=5,
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
        raw_picks_json=_raw_picks_json(),
        raw_transfers_json=None,
        is_current=False,
        superseded_at=None,
    )
    db_session.add(snapshot)
    db_session.commit()
    db_session.refresh(snapshot)
    return snapshot


def test_h2h_simulate_happy_path(
    db_session, patched_fpl_client_optimization_with_active_gameweek, patched_fpl_client_league
):
    season_id = _seed(db_session)
    _run_league_sync(db_session)
    rival_profile = db_session.execute(
        select(RivalProfile).where(
            RivalProfile.season_id == season_id, RivalProfile.fpl_manager_id == LEAGUE_RIVAL_B_FPL_MANAGER_ID
        )
    ).scalar_one()
    snapshot = _create_manual_snapshot(db_session, season_id)
    client = authed_client()

    response = client.post(
        "/h2h/simulate",
        json={
            "manager_squad_snapshot_id": str(snapshot.id),
            "rival_profile_id": str(rival_profile.id),
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["run_status"] == "succeeded"
    assert body["sample_count"] == 5000
    assert body["confidence"] == "low"  # honest cold-start floor, matches the rival profile's own
    total_probability = (
        float(body["win_probability"]) + float(body["draw_probability"]) + float(body["loss_probability"])
    )
    assert total_probability == pytest.approx(1.0, abs=1e-6)
    assert float(body["user_score_mean"]) >= 0
    assert float(body["rival_score_mean"]) >= 0


def test_h2h_simulate_missing_rival_profile_returns_409(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    season_id = _seed(db_session)
    snapshot = _create_manual_snapshot(db_session, season_id)
    client = authed_client()

    response = client.post(
        "/h2h/simulate",
        json={
            "manager_squad_snapshot_id": str(snapshot.id),
            "rival_profile_id": str(uuid.uuid4()),
        },
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error_code"] == "RIVAL_PROFILE_NOT_FOUND"
    assert "job_id" in body


def test_h2h_simulate_rejects_missing_body_fields_with_422(db_session):
    client = authed_client()

    response = client.post("/h2h/simulate", json={})

    assert response.status_code == 422


# --- GET /h2h/simulate/{id} (Milestone 8 Layer 1) ----------------------------------------------


def test_get_h2h_simulation_run_returns_the_same_body_the_post_returned(
    db_session, patched_fpl_client_optimization_with_active_gameweek, patched_fpl_client_league
):
    season_id = _seed(db_session)
    _run_league_sync(db_session)
    rival_profile = db_session.execute(
        select(RivalProfile).where(
            RivalProfile.season_id == season_id, RivalProfile.fpl_manager_id == LEAGUE_RIVAL_B_FPL_MANAGER_ID
        )
    ).scalar_one()
    snapshot = _create_manual_snapshot(db_session, season_id)
    client = authed_client()
    created = client.post(
        "/h2h/simulate",
        json={
            "manager_squad_snapshot_id": str(snapshot.id),
            "rival_profile_id": str(rival_profile.id),
        },
    ).json()

    response = client.get(f"/h2h/simulate/{created['h2h_simulation_run_id']}")

    assert response.status_code == 200
    assert response.json() == created


def test_get_h2h_simulation_run_unknown_id_returns_404():
    client = authed_client()

    response = client.get(f"/h2h/simulate/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["error_code"] == "H2H_SIMULATION_RUN_NOT_FOUND"


# --- GET /h2h/simulate (list/history, Milestone 8 item 1) -----------------------------------


def test_list_h2h_simulation_runs_filters_by_snapshot(
    db_session, patched_fpl_client_optimization_with_active_gameweek, patched_fpl_client_league
):
    season_id = _seed(db_session)
    _run_league_sync(db_session)
    rival_profile = db_session.execute(
        select(RivalProfile).where(
            RivalProfile.season_id == season_id, RivalProfile.fpl_manager_id == LEAGUE_RIVAL_B_FPL_MANAGER_ID
        )
    ).scalar_one()
    snapshot = _create_manual_snapshot(db_session, season_id)
    client = authed_client()
    created = client.post(
        "/h2h/simulate",
        json={"manager_squad_snapshot_id": str(snapshot.id), "rival_profile_id": str(rival_profile.id)},
    ).json()

    response = client.get("/h2h/simulate", params={"manager_squad_snapshot_id": str(snapshot.id)})

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["h2h_simulation_run_id"] == created["h2h_simulation_run_id"]
    assert body[0]["win_probability"] == created["win_probability"]
    assert "strategy_candidates" not in body[0]


def test_list_h2h_simulation_runs_empty_for_unknown_snapshot():
    client = authed_client()

    response = client.get("/h2h/simulate", params={"manager_squad_snapshot_id": str(uuid.uuid4())})

    assert response.status_code == 200
    assert response.json() == []
