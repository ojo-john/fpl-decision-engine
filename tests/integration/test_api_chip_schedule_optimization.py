"""Integration tests for the /optimize/chips/schedule endpoints against a real database.

Mirrors ``test_api_chip_optimization.py``'s own "duplicate a minimal setup rather than import
from the job-level test module" discipline -- this module tests the HTTP layer specifically
(request/response shape, error-to-409/404 mapping, route-ordering), not the real evaluation logic,
already covered in depth by ``test_chip_schedule_optimization_job.py``. Phase 2's own real
structural change: the request body is now ``manager_squad_snapshot_id`` (mirrors
``ChipSimulationRequest``'s own shape) -- there is no longer an existing ``CHIP_SIMULATION`` run
to point at instead.
"""

import uuid

import pytest
from sqlalchemy import delete, select

from app.jobs.service import execute_job
from packages.database.models.chip_schedule_optimization_run import ChipScheduleOptimizationRun
from packages.database.models.manager_squad_snapshot import ManagerSquadSnapshot
from packages.database.models.player import Player
from packages.database.models.season import Season
from packages.domain.jobs import JobStatus, JobType, TriggerType
from packages.domain.transfers import ManagerSquadSnapshotSource
from tests.integration.conftest import authed_client


@pytest.fixture(autouse=True)
def _cleanup_chip_schedule_optimization_runs(db_session):
    """See test_chip_schedule_optimization_job.py's own identical fixture for the full real
    incident this guards against (a shared-DB alembic-downgrade failure)."""
    db_session.execute(delete(ChipScheduleOptimizationRun))
    db_session.commit()
    yield
    db_session.execute(delete(ChipScheduleOptimizationRun))
    db_session.commit()


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


def _seed(db_session):
    ingestion_job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert ingestion_job.status == JobStatus.SUCCEEDED
    season_id = db_session.execute(
        select(Season.id).where(Season.label == ingestion_job.output_json["season_label"])
    ).scalar_one()
    return season_id


def _raw_picks_json() -> dict:
    picks = []
    for position, element in enumerate(_STARTING_ELEMENT_IDS, start=1):
        picks.append(
            {
                "element": element,
                "position": position,
                "multiplier": 2 if element == _CAPTAIN_ELEMENT_ID else 1,
                "is_captain": element == _CAPTAIN_ELEMENT_ID,
                "is_vice_captain": False,
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
        raw_picks_json=_raw_picks_json(),
        raw_transfers_json=None,
        is_current=False,
        superseded_at=None,
    )
    db_session.add(snapshot)
    db_session.commit()
    db_session.refresh(snapshot)
    return snapshot


def test_chip_schedule_optimization_happy_path(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    """The active-gameweek fixture's only unfinished gameweek (event 38, window 2) is enough for a
    real, if small, end-to-end evaluation -- request/response shape is this module's own concern,
    not the real evaluation math (see test_chip_schedule_optimization_job.py for that)."""
    season_id = _seed(db_session)
    snapshot = _create_manual_snapshot(db_session, season_id)
    client = authed_client()

    response = client.post("/optimize/chips/schedule", json={"manager_squad_snapshot_id": str(snapshot.id)})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run_status"] == "succeeded"
    assert body["manager_squad_snapshot_id"] == str(snapshot.id)
    # Phase 2 never has a source CHIP_SIMULATION run (migration 0048 made this nullable).
    assert body["source_chip_optimization_run_id"] is None
    assert isinstance(body["assignments"], list)


def test_chip_schedule_optimization_missing_snapshot_id_returns_422(db_session):
    client = authed_client()

    response = client.post("/optimize/chips/schedule", json={})

    assert response.status_code == 422


def test_chip_schedule_optimization_unknown_snapshot_returns_409(db_session):
    client = authed_client()

    response = client.post("/optimize/chips/schedule", json={"manager_squad_snapshot_id": str(uuid.uuid4())})

    assert response.status_code == 409
    body = response.json()
    assert body["error_code"] == "SNAPSHOT_NOT_FOUND"
    assert "job_id" in body


def test_get_chip_schedule_optimization_run_returns_the_same_body_the_post_returned(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    season_id = _seed(db_session)
    snapshot = _create_manual_snapshot(db_session, season_id)
    client = authed_client()
    created = client.post(
        "/optimize/chips/schedule", json={"manager_squad_snapshot_id": str(snapshot.id)}
    ).json()

    response = client.get(f"/optimize/chips/schedule/{created['chip_schedule_optimization_run_id']}")

    assert response.status_code == 200
    assert response.json() == created


def test_get_chip_schedule_optimization_run_unknown_id_returns_404():
    client = authed_client()

    response = client.get(f"/optimize/chips/schedule/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["error_code"] == "CHIP_SCHEDULE_OPTIMIZATION_RUN_NOT_FOUND"


def test_list_chip_schedule_optimization_runs_filters_by_snapshot(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    season_id = _seed(db_session)
    snapshot = _create_manual_snapshot(db_session, season_id)
    client = authed_client()
    created = client.post(
        "/optimize/chips/schedule", json={"manager_squad_snapshot_id": str(snapshot.id)}
    ).json()

    response = client.get("/optimize/chips/schedule", params={"manager_squad_snapshot_id": str(snapshot.id)})

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["chip_schedule_optimization_run_id"] == created["chip_schedule_optimization_run_id"]
    assert "assignments" not in body[0]


def test_list_chip_schedule_optimization_runs_empty_for_unknown_snapshot():
    client = authed_client()

    response = client.get("/optimize/chips/schedule", params={"manager_squad_snapshot_id": str(uuid.uuid4())})

    assert response.status_code == 200
    assert response.json() == []


def test_chip_schedule_route_still_resolves_before_the_run_id_route(db_session):
    """Route-ordering regression guard: /optimize/chips/schedule (and /windows) must stay
    registered before the one-segment /optimize/chips/{chip_optimization_run_id} catch-all, or
    Starlette would try to parse "schedule"/"windows" as a UUID path parameter and 422 instead of
    reaching the real handler."""
    client = authed_client()

    response = client.get("/optimize/chips/schedule")

    assert response.status_code == 200


def test_get_chip_optimization_run_includes_manager_squad_snapshot_id(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    """A real regression guard for the field the frontend's own joint-schedule button now depends
    on (POST /optimize/chips/schedule takes manager_squad_snapshot_id, not a source run id -- the
    chip run detail page has to get it from here)."""
    season_id = _seed(db_session)
    snapshot = _create_manual_snapshot(db_session, season_id)
    client = authed_client()

    response = client.post("/optimize/chips", json={"manager_squad_snapshot_id": str(snapshot.id)})

    assert response.status_code == 200
    assert response.json()["manager_squad_snapshot_id"] == str(snapshot.id)
