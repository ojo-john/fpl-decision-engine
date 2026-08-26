"""Integration tests for the /optimize/chips/schedule endpoints against a real database.

Mirrors ``test_api_chip_optimization.py``'s own "duplicate a minimal setup rather than import
from the job-level test module" discipline -- this module tests the HTTP layer specifically
(request/response shape, error-to-409/404 mapping, route-ordering), not the job's own assignment
logic, which is already covered in depth by ``test_chip_schedule_optimization_job.py``. Source
``ChipOptimizationRun``/``ChipWindowCandidate`` rows are hand-seeded directly via the ORM (not a
real CHIP_SIMULATION run) for the same reason that module does it -- fast, deterministic control
over gain values, since this job only ever reads those two tables.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from app.jobs.service import execute_job
from packages.database.models.chip_optimization_run import ChipOptimizationRun
from packages.database.models.chip_window_candidate import ChipWindowCandidate
from packages.database.models.manager_squad_snapshot import ManagerSquadSnapshot
from packages.database.models.player import Player
from packages.database.models.rules_config import RulesConfig
from packages.database.models.season import Season
from packages.domain.chips import ChipOptimizationRunStatus, ChipType
from packages.domain.jobs import JobStatus, JobType, TriggerType
from packages.domain.rules import RulesStatus
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


def _seed_source_chip_optimization_run(db_session, snapshot: ManagerSquadSnapshot) -> ChipOptimizationRun:
    rules_config_id = db_session.execute(
        select(RulesConfig.id).where(
            RulesConfig.season_id == snapshot.season_id, RulesConfig.is_current.is_(True)
        )
    ).scalar_one()
    now = datetime.now(UTC)
    run = ChipOptimizationRun(
        manager_squad_snapshot_id=snapshot.id,
        season_id=snapshot.season_id,
        rules_config_id=rules_config_id,
        run_status=ChipOptimizationRunStatus.SUCCEEDED,
        target_fpl_event_id=2,
        projection_formula_version=1,
        rules_status_snapshot=RulesStatus.OK,
        optimization_permitted_snapshot=True,
        started_at=now,
        completed_at=now,
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)

    db_session.add_all(
        [
            ChipWindowCandidate(
                chip_optimization_run_id=run.id,
                chip_type=ChipType.FREEHIT,
                window_number=1,
                candidate_fpl_event_id=2,
                gain_mean=Decimal("3.3"),
                gain_floor=Decimal("3.3"),
                gain_ceiling=Decimal("3.3"),
                rank_within_chip_type=1,
            ),
            ChipWindowCandidate(
                chip_optimization_run_id=run.id,
                chip_type=ChipType.TRIPLE_CAPTAIN,
                window_number=1,
                candidate_fpl_event_id=2,
                gain_mean=Decimal("7.3"),
                gain_floor=Decimal("7.3"),
                gain_ceiling=Decimal("7.3"),
                rank_within_chip_type=1,
            ),
        ]
    )
    db_session.commit()
    return run


def test_chip_schedule_optimization_happy_path(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    season_id = _seed(db_session)
    snapshot = _create_manual_snapshot(db_session, season_id)
    source_run = _seed_source_chip_optimization_run(db_session, snapshot)
    client = authed_client()

    response = client.post("/optimize/chips/schedule", json={"chip_optimization_run_id": str(source_run.id)})

    assert response.status_code == 200
    body = response.json()
    assert body["run_status"] == "succeeded"
    assert body["source_chip_optimization_run_id"] == str(source_run.id)
    assert float(body["total_expected_gain"]) == 7.3
    assignments_by_chip = {a["chip_type"]: a for a in body["assignments"]}
    assert assignments_by_chip["3xc"]["assigned_fpl_event_id"] == 2
    assert assignments_by_chip["freehit"]["assigned_fpl_event_id"] is None


def test_chip_schedule_optimization_auto_resolves_when_no_id_given(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    season_id = _seed(db_session)
    snapshot = _create_manual_snapshot(db_session, season_id)
    source_run = _seed_source_chip_optimization_run(db_session, snapshot)
    client = authed_client()

    response = client.post("/optimize/chips/schedule", json={})

    assert response.status_code == 200
    assert response.json()["source_chip_optimization_run_id"] == str(source_run.id)


def test_chip_schedule_optimization_unknown_source_run_returns_409(db_session):
    client = authed_client()

    response = client.post("/optimize/chips/schedule", json={"chip_optimization_run_id": str(uuid.uuid4())})

    assert response.status_code == 409
    body = response.json()
    assert body["error_code"] == "CHIP_OPTIMIZATION_RUN_NOT_FOUND"
    assert "job_id" in body


def test_get_chip_schedule_optimization_run_returns_the_same_body_the_post_returned(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    season_id = _seed(db_session)
    snapshot = _create_manual_snapshot(db_session, season_id)
    source_run = _seed_source_chip_optimization_run(db_session, snapshot)
    client = authed_client()
    created = client.post(
        "/optimize/chips/schedule", json={"chip_optimization_run_id": str(source_run.id)}
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
    source_run = _seed_source_chip_optimization_run(db_session, snapshot)
    client = authed_client()
    created = client.post(
        "/optimize/chips/schedule", json={"chip_optimization_run_id": str(source_run.id)}
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


def test_chip_windows_route_still_resolves_before_the_run_id_route(db_session):
    """Route-ordering regression guard: /optimize/chips/schedule (and /windows) must stay
    registered before the one-segment /optimize/chips/{chip_optimization_run_id} catch-all, or
    Starlette would try to parse "schedule"/"windows" as a UUID path parameter and 422 instead of
    reaching the real handler."""
    client = authed_client()

    response = client.get("/optimize/chips/schedule")

    assert response.status_code == 200
