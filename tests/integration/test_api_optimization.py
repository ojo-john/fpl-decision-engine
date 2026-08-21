"""Integration tests for the /optimize/* endpoints against a real database.

Seeds data via ``execute_job`` on ``db_session``, exercises the HTTP layer via
``TestClient`` (which uses its own request-scoped session through
``apps.api.dependencies.get_db`` — both point at the same real Postgres).
"""

import uuid

from sqlalchemy import select

from app.jobs.service import execute_job
from packages.database.models.ingestion_run import IngestionRun
from packages.database.models.job import Job
from packages.database.models.manager_squad_snapshot import ManagerSquadSnapshot
from packages.database.models.optimization_run import OptimizationRun
from packages.database.models.player import Player
from packages.database.models.position import Position
from packages.database.models.season import Season
from packages.domain.jobs import JobStatus, JobType, TriggerType
from packages.domain.transfers import ManagerSquadSnapshotSource
from tests.integration.conftest import authed_client


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
    client = authed_client()

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

    # price_momentum is deliberately never populated on a real, persisted run -- it's a live,
    # "right now" signal, and this response could be re-fetched long after the run happened
    # (see OptimizationRunPlayerOut.price_momentum's own docstring). Only /optimize/preview
    # populates it.
    assert all(p["price_momentum"] is None for p in body["players"])


def test_optimize_initial_squad_refused_returns_409(db_session, patched_fpl_client_optimization):
    ingestion_job = _seed(db_session)
    ingestion_run_id = uuid.UUID(ingestion_job.output_json["ingestion_run_id"])
    ingestion_run = db_session.get(IngestionRun, ingestion_run_id)
    ingestion_run.optimization_permitted = False
    db_session.commit()

    client = authed_client()
    response = client.post("/optimize/initial-squad", json={})

    assert response.status_code == 409
    body = response.json()
    assert body["error_code"] == "OPTIMIZATION_NOT_PERMITTED"
    assert "job_id" in body


# --- POST /optimize/preview (unofficial, gate-bypassing squad preview) ---------------------


def test_preview_squad_works_even_when_the_real_gate_is_refused(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    """The whole point of /optimize/preview: unlike /optimize/initial-squad, it must still
    solve successfully even when optimization_permitted is False."""
    ingestion_job = _seed(db_session)
    ingestion_run_id = uuid.UUID(ingestion_job.output_json["ingestion_run_id"])
    ingestion_run = db_session.get(IngestionRun, ingestion_run_id)
    ingestion_run.optimization_permitted = False
    db_session.commit()

    client = authed_client()
    jobs_before = db_session.execute(select(Job.id)).scalars().all()
    runs_before = db_session.execute(select(OptimizationRun.id)).scalars().all()

    response = client.post("/optimize/preview")

    assert response.status_code == 200
    body = response.json()
    assert body["solver_status"] == "optimal"
    assert len(body["players"]) == 15
    assert body["squad_total_cost"] is not None
    assert body["captain_player_id"] is not None
    assert body["vice_captain_player_id"] is not None
    # Every previewed player gets a live price_momentum classification -- the highest-value
    # spot for it, since this is exactly where a manager is choosing their 15 (see
    # apps.api.schemas.optimization.OptimizationRunPlayerOut's own docstring for why this is
    # populated on /optimize/preview specifically but never on a persisted run).
    for player in body["players"]:
        assert player["price_momentum"] is not None
        assert player["price_momentum"]["momentum"] in {"likely_rise", "likely_fall", "stable", "unknown"}

    # Never persisted: not a Job, not an optimization_runs row.
    db_session.expire_all()
    jobs_after = db_session.execute(select(Job.id)).scalars().all()
    runs_after = db_session.execute(select(OptimizationRun.id)).scalars().all()
    assert set(jobs_after) == set(jobs_before)
    assert set(runs_after) == set(runs_before)


def test_preview_squad_flags_a_cold_start_gameweek(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    """No player_gameweek_stats exist yet for a freshly-ingested season (the _seed fixture only
    ingests players/teams/fixtures, never any gameweek stats) -- is_cold_start_gameweek must be
    True so the caller knows to show the "point values aren't trustworthy yet" warning."""
    _seed(db_session)
    client = authed_client()

    response = client.post("/optimize/preview")

    assert response.status_code == 200
    assert response.json()["is_cold_start_gameweek"] is True


def test_preview_squad_returns_composition_risk_on_every_call(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    """composition_risk (app.optimization.squad_risk) has no request-side trigger -- it's
    computed on every /optimize/preview call, since the solver's own objective has no notion
    of budget concentration or bench depth and would otherwise never surface either."""
    _seed(db_session)
    client = authed_client()

    response = client.post("/optimize/preview")

    assert response.status_code == 200
    risk = response.json()["composition_risk"]
    assert risk is not None
    assert isinstance(risk["budget_share_pct"], float)
    assert risk["most_expensive_player_id"] is not None
    assert isinstance(risk["is_budget_notably_concentrated"], bool)
    assert isinstance(risk["bench_depth_warnings"], list)


def test_preview_squad_exclude_player_ids_produces_a_different_squad(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    """exclude_player_ids re-solves with that player removed from the candidate pool entirely
    -- the concrete "what if I didn't have this player" comparison a manager would otherwise
    only get via a one-off manual re-solve."""
    _seed(db_session)
    client = authed_client()

    baseline = client.post("/optimize/preview")
    assert baseline.status_code == 200
    excluded_id = baseline.json()["captain_player_id"]
    assert excluded_id is not None

    response = client.post("/optimize/preview", json={"exclude_player_ids": [excluded_id]})

    assert response.status_code == 200
    body = response.json()
    assert body["excluded_player_ids"] == [excluded_id]
    assert excluded_id not in {p["player_id"] for p in body["players"]}
    # The fixture's 60-player pool has generous depth per position (see
    # build_optimization_bootstrap_json's own docstring) -- removing exactly one candidate
    # must still yield a full, feasible 15-man squad, not a degraded one.
    assert len(body["players"]) == 15
    assert body["solver_status"] == "optimal"


def test_preview_squad_exclude_player_ids_ignores_an_id_not_in_the_candidate_pool(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    """A garbage/unrelated id in exclude_player_ids must not crash the solve -- it simply has
    no matching candidate to filter out."""
    _seed(db_session)
    client = authed_client()
    unrelated_id = str(uuid.uuid4())

    response = client.post("/optimize/preview", json={"exclude_player_ids": [unrelated_id]})

    assert response.status_code == 200
    body = response.json()
    assert body["excluded_player_ids"] == [unrelated_id]
    assert body["solver_status"] == "optimal"
    assert len(body["players"]) == 15


def test_preview_squad_excluding_an_entire_position_degrades_gracefully_to_infeasible(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    """Excluding every FWD candidate leaves no way to fill the FWD squad_select requirement --
    must come back as a clean 200 with solver_status=infeasible and empty fields, never a
    500 or an unhandled exception."""
    _seed(db_session)
    season_id = db_session.execute(select(Season.id).where(Season.is_current.is_(True))).scalar_one()
    fwd_player_ids = (
        db_session.execute(
            select(Player.id)
            .join(Position, Player.position_id == Position.id)
            .where(Position.short_name == "FWD", Player.season_id == season_id)
        )
        .scalars()
        .all()
    )
    assert len(fwd_player_ids) > 0
    client = authed_client()

    response = client.post(
        "/optimize/preview", json={"exclude_player_ids": [str(pid) for pid in fwd_player_ids]}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["solver_status"] == "infeasible"
    assert body["players"] == []
    assert body["squad_total_cost"] is None
    assert body["composition_risk"]["budget_share_pct"] is None
    assert body["composition_risk"]["bench_depth_warnings"] == []


def test_optimize_lineup_chained_from_squad_run(db_session, patched_fpl_client_optimization):
    _seed(db_session)
    client = authed_client()

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
    client = authed_client()
    squad_run = client.post("/optimize/initial-squad", json={}).json()
    player_ids = [p["player_id"] for p in squad_run["players"]]

    response = client.post("/optimize/lineup", json={"player_ids": player_ids})

    assert response.status_code == 200
    body = response.json()
    assert len([p for p in body["players"] if p["is_starting"]]) == 11


def test_optimize_lineup_rejects_ambiguous_input_with_422(db_session, patched_fpl_client_optimization):
    _seed(db_session)
    client = authed_client()

    response = client.post(
        "/optimize/lineup",
        json={"source_optimization_run_id": str(uuid.uuid4()), "player_ids": [str(uuid.uuid4())]},
    )

    assert response.status_code == 422


def test_optimize_lineup_rejects_missing_input_with_422(db_session, patched_fpl_client_optimization):
    _seed(db_session)
    client = authed_client()

    response = client.post("/optimize/lineup", json={})

    assert response.status_code == 422


def test_optimize_transfers_happy_path(db_session, patched_fpl_client_optimization_with_active_gameweek):
    season_id = _seed_with_active_gameweek(db_session)
    snapshot = _create_manual_snapshot(db_session, season_id)
    client = authed_client()

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

    client = authed_client()
    response = client.post("/optimize/transfers", json={"manager_squad_snapshot_id": str(snapshot.id)})

    assert response.status_code == 409
    body = response.json()
    assert body["error_code"] == "OPTIMIZATION_NOT_PERMITTED"
    assert "job_id" in body


def test_optimize_transfers_unknown_snapshot_returns_409(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    _seed_with_active_gameweek(db_session)
    client = authed_client()

    response = client.post("/optimize/transfers", json={"manager_squad_snapshot_id": str(uuid.uuid4())})

    assert response.status_code == 409
    body = response.json()
    assert body["error_code"] == "SNAPSHOT_NOT_FOUND"


# --- GET /optimize/{id}, GET /optimize/transfers/{id} (Milestone 8 Layer 1) -----------------


def test_get_optimization_run_returns_the_same_body_the_post_returned(
    db_session, patched_fpl_client_optimization
):
    _seed(db_session)
    client = authed_client()
    created = client.post("/optimize/initial-squad", json={}).json()

    response = client.get(f"/optimize/{created['optimization_run_id']}")

    assert response.status_code == 200
    assert response.json() == created


def test_get_optimization_run_unknown_id_returns_404():
    client = authed_client()

    response = client.get(f"/optimize/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["error_code"] == "OPTIMIZATION_RUN_NOT_FOUND"


def test_get_transfer_optimization_run_returns_the_same_body_the_post_returned(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    season_id = _seed_with_active_gameweek(db_session)
    snapshot = _create_manual_snapshot(db_session, season_id)
    client = authed_client()
    created = client.post("/optimize/transfers", json={"manager_squad_snapshot_id": str(snapshot.id)}).json()

    response = client.get(f"/optimize/transfers/{created['transfer_optimization_run_id']}")

    assert response.status_code == 200
    assert response.json() == created


def test_get_transfer_optimization_run_unknown_id_returns_404():
    client = authed_client()

    response = client.get(f"/optimize/transfers/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["error_code"] == "TRANSFER_OPTIMIZATION_RUN_NOT_FOUND"


# --- GET /optimize, GET /optimize/transfers (list/history, Milestone 8 item 1) --------------


def test_list_optimization_runs_includes_created_run(db_session, patched_fpl_client_optimization):
    _seed(db_session)
    client = authed_client()
    created = client.post("/optimize/initial-squad", json={}).json()
    # POST /optimize/initial-squad names the endpoint after its INPUT (start from nothing),
    # but the run it produces recommends a squad AND its starting XI/captain together in one
    # shot -- so its own recommendation_type is "joint_squad_and_xi", not "initial_squad" (that
    # value is reserved for a recommendation_type this job never actually sets; see
    # packages.domain.optimization.RecommendationType).
    assert created["recommendation_type"] == "joint_squad_and_xi"

    response = client.get("/optimize", params={"recommendation_type": "joint_squad_and_xi"})

    assert response.status_code == 200
    body = response.json()
    # Filtered by recommendation_type only (not manager_squad_snapshot_id -- OptimizationRun
    # has no such column), so other tests' runs of the SAME type may legitimately also be
    # present in this shared, non-truncated database; find this test's own row rather than
    # asserting exact list equality/length.
    matching = next(r for r in body if r["optimization_run_id"] == created["optimization_run_id"])
    assert matching["recommendation_type"] == "joint_squad_and_xi"
    assert matching["run_status"] == "succeeded"
    assert matching["squad_total_cost"] == created["squad_total_cost"]
    assert matching["squad_projected_points"] == created["squad_projected_points"]
    assert "players" not in matching


def test_list_transfer_optimization_runs_filters_by_snapshot(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    season_id = _seed_with_active_gameweek(db_session)
    snapshot = _create_manual_snapshot(db_session, season_id)
    client = authed_client()
    created = client.post("/optimize/transfers", json={"manager_squad_snapshot_id": str(snapshot.id)}).json()

    response = client.get("/optimize/transfers", params={"manager_squad_snapshot_id": str(snapshot.id)})

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["transfer_optimization_run_id"] == created["transfer_optimization_run_id"]
    assert body[0]["recommended_action"] == created["recommended_action"]
    assert "scenarios" not in body[0]


def test_list_transfer_optimization_runs_empty_for_unknown_snapshot():
    client = authed_client()

    response = client.get("/optimize/transfers", params={"manager_squad_snapshot_id": str(uuid.uuid4())})

    assert response.status_code == 200
    assert response.json() == []
