"""Integration tests for the TRANSFER_OPTIMIZATION job against a real database.

Uses a directly-constructed MANUAL_ENTRY ``ManagerSquadSnapshot`` row (rather
than a MANAGER_SYNC one) so the exact expected recommendation is fully
hand-verifiable -- same rationale as Layer 2's own scenario-search unit tests
building precise, controlled scenarios rather than relying on real-world
data. The 15-player squad and its bank/free-transfers are picked from
``build_optimization_bootstrap_json``'s ~60-player synthetic pool (see
conftest.py) with the exact ``ep_next``/cost/status combination worked out
by hand (and cross-checked with a standalone brute-force script) so that:

- the single best 1-transfer swap is OUT=element 30011 (DEF, ep_next=3.0,
  now_cost=54) -> IN=element 30021 (DEF, ep_next=8.5, now_cost=42), gain=5.5,
  with no other (out, in) pair tying that gain;
- the single best 2-transfer combo is OUT={30001 (GKP, ep=3.5, cost=45),
  30011 (DEF, ep=3.0, cost=54)} -> IN={30009 (GKP, ep=8.0, cost=48), 30021
  (DEF, ep=8.5, cost=42)}, gain=10.0, again with no tie -- and deliberately a
  cross-position (GKP+DEF) combo so the persisted move-to-move pairing is
  unambiguous (a same-position 2-transfer tie would leave the specific
  out->in pairing dependent on internal UUID sort order, not the test's own
  design).
- with ``free_transfers_available=2``, hit cost is 0 for both the 1- and
  2-transfer scenarios, so ``net_horizon_points_gain`` is directly the raw
  gain above and the 2-transfer scenario (10.0) clearly beats the 1-transfer
  (5.5) and 0-transfer (0.0) scenarios -- an unambiguous recommendation.

Requires ``patched_fpl_client_optimization_with_active_gameweek`` (not the
plain ``patched_fpl_client_optimization``) since horizon resolution needs at
least one unfinished gameweek to build a horizon against at all.
"""

import uuid

import pytest
from sqlalchemy import select

from app.jobs.service import execute_job
from app.optimization.constraints import load_optimization_constraints
from packages.database.models.ingestion_run import IngestionRun
from packages.database.models.manager_squad_snapshot import ManagerSquadSnapshot
from packages.database.models.player import Player
from packages.database.models.rules_config import RulesConfig
from packages.database.models.season import Season
from packages.database.models.transfer_optimization_run import TransferOptimizationRun
from packages.database.models.transfer_optimization_run_move import TransferOptimizationRunMove
from packages.database.models.transfer_optimization_run_scenario import TransferOptimizationRunScenario
from packages.domain.jobs import JobStatus, JobType, TriggerType
from packages.domain.rules import RulesStatus
from packages.domain.transfers import ManagerSquadSnapshotSource, TransferOptimizationRunStatus

# GKP: 30001, 30002 | DEF: 30010, 30011, 30019, 30014, 30016
# MID: 30028, 30029, 30030, 30031, 30032 | FWD: 30048, 30049, 30050
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
_BANK = 20
_FREE_TRANSFERS_AVAILABLE = 2
_EXPECTED_HIT_COST_MAGNITUDE = 4

_EXPECTED_ONE_TRANSFER_OUT_ELEMENT = 30011
_EXPECTED_ONE_TRANSFER_IN_ELEMENT = 30021
_EXPECTED_ONE_TRANSFER_GAIN = 5.5

_EXPECTED_TWO_TRANSFER_MOVES = [
    (30001, 30009, 4.5),  # GKP: ep 3.5 -> 8.0
    (30011, 30021, 5.5),  # DEF: ep 3.0 -> 8.5
]
_EXPECTED_TWO_TRANSFER_GAIN = 10.0


def _seed_players_with_active_gameweek(db_session):
    ingestion_job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert ingestion_job.status == JobStatus.SUCCEEDED
    season_id = db_session.execute(
        select(Season.id).where(Season.label == ingestion_job.output_json["season_label"])
    ).scalar_one()
    return season_id


def _player_id_by_element(db_session, season_id, element_id: int) -> uuid.UUID:
    return db_session.execute(
        select(Player.id).where(Player.season_id == season_id, Player.fpl_element_id == element_id)
    ).scalar_one()


def _create_manual_snapshot(db_session, season_id) -> ManagerSquadSnapshot:
    player_by_element = {
        eid: db_session.execute(
            select(Player).where(Player.season_id == season_id, Player.fpl_element_id == eid)
        ).scalar_one()
        for eid in _SQUAD_ELEMENT_IDS
    }

    squad_player_ids = [str(player_by_element[eid].id) for eid in _SQUAD_ELEMENT_IDS]
    # No profit assumed -- selling price = purchase price = now_cost, same
    # conservative convention MANAGER_SYNC/manual-squad endpoint both use for
    # a player with no derivable purchase price.
    price_by_player = {
        str(player_by_element[eid].id): player_by_element[eid].now_cost for eid in _SQUAD_ELEMENT_IDS
    }

    snapshot = ManagerSquadSnapshot(
        season_id=season_id,
        source=ManagerSquadSnapshotSource.MANUAL_ENTRY,
        fpl_manager_id=None,
        job_id=None,
        as_of_gameweek_id=None,
        bank=_BANK,
        free_transfers_available=_FREE_TRANSFERS_AVAILABLE,
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


def _run_transfer_optimization(db_session, snapshot_id: uuid.UUID, horizon_gameweeks: int = 6):
    return execute_job(
        db_session,
        job_type=JobType.TRANSFER_OPTIMIZATION,
        parameters={
            "manager_squad_snapshot_id": str(snapshot_id),
            "horizon_gameweeks": horizon_gameweeks,
        },
        requested_by="integration-test",
        trigger_type=TriggerType.LOCAL,
    )


def test_transfer_optimization_recommends_two_transfers_with_full_scenario_persistence(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    season_id = _seed_players_with_active_gameweek(db_session)
    snapshot = _create_manual_snapshot(db_session, season_id)

    job = _run_transfer_optimization(db_session, snapshot.id)

    assert job.status == JobStatus.SUCCEEDED
    output = job.output_json
    assert output["recommended_transfers_count"] == 2
    assert output["recommended_hit_cost"] == 0
    assert output["recommended_net_horizon_points_gain"] == pytest.approx(_EXPECTED_TWO_TRANSFER_GAIN)

    run_id = uuid.UUID(output["transfer_optimization_run_id"])
    run_row = db_session.get(TransferOptimizationRun, run_id)
    assert run_row is not None
    assert run_row.manager_squad_snapshot_id == snapshot.id
    assert run_row.season_id == season_id
    assert run_row.run_status == TransferOptimizationRunStatus.SUCCEEDED
    assert run_row.free_transfers_available_snapshot == _FREE_TRANSFERS_AVAILABLE
    assert run_row.bank_snapshot == _BANK
    assert run_row.transfer_hit_cost_snapshot == _EXPECTED_HIT_COST_MAGNITUDE
    assert run_row.horizon_length == 6
    # Only one unfinished gameweek exists in this fixture -- the horizon
    # gracefully truncates to length 1, matching resolve_horizon_gameweeks's
    # own documented near-season-end behavior.
    assert len(run_row.horizon_gameweek_ids_json) == 1
    assert run_row.horizon_start_gameweek_id is not None
    assert run_row.recommended_transfers_count == 2
    assert run_row.recommended_hit_cost == 0
    assert float(run_row.recommended_net_horizon_points_gain) == pytest.approx(_EXPECTED_TWO_TRANSFER_GAIN)
    assert run_row.optimization_permitted_snapshot is True
    assert run_row.completed_at is not None

    # Never trust the returned summary alone -- re-derive everything from the
    # persisted scenario/move rows, matching every other optimizer job's own
    # test discipline in this codebase.
    scenario_rows = (
        db_session.execute(
            select(TransferOptimizationRunScenario)
            .where(TransferOptimizationRunScenario.transfer_optimization_run_id == run_id)
            .order_by(TransferOptimizationRunScenario.transfers_count)
        )
        .scalars()
        .all()
    )
    assert len(scenario_rows) == 3
    assert [s.transfers_count for s in scenario_rows] == [0, 1, 2]

    recommended_rows = [s for s in scenario_rows if s.is_recommended]
    assert len(recommended_rows) == 1
    assert recommended_rows[0].transfers_count == 2

    scenario0, scenario1, scenario2 = scenario_rows

    assert float(scenario0.raw_horizon_points_gain) == pytest.approx(0.0)
    assert scenario0.hit_cost == 0
    assert float(scenario0.net_horizon_points_gain) == pytest.approx(0.0)
    assert scenario0.is_recommended is False

    assert float(scenario1.raw_horizon_points_gain) == pytest.approx(_EXPECTED_ONE_TRANSFER_GAIN)
    assert scenario1.hit_cost == 0
    assert float(scenario1.net_horizon_points_gain) == pytest.approx(_EXPECTED_ONE_TRANSFER_GAIN)
    assert scenario1.is_recommended is False

    assert float(scenario2.raw_horizon_points_gain) == pytest.approx(_EXPECTED_TWO_TRANSFER_GAIN)
    assert scenario2.hit_cost == 0
    assert float(scenario2.net_horizon_points_gain) == pytest.approx(_EXPECTED_TWO_TRANSFER_GAIN)
    assert scenario2.is_recommended is True

    # 0-transfer scenario has no moves at all.
    moves0 = (
        db_session.execute(
            select(TransferOptimizationRunMove).where(
                TransferOptimizationRunMove.transfer_optimization_run_scenario_id == scenario0.id
            )
        )
        .scalars()
        .all()
    )
    assert moves0 == []

    # 1-transfer scenario has exactly the hand-verified best swap.
    moves1 = (
        db_session.execute(
            select(TransferOptimizationRunMove)
            .where(TransferOptimizationRunMove.transfer_optimization_run_scenario_id == scenario1.id)
            .order_by(TransferOptimizationRunMove.move_index)
        )
        .scalars()
        .all()
    )
    assert len(moves1) == 1
    expected_out_id = _player_id_by_element(db_session, season_id, _EXPECTED_ONE_TRANSFER_OUT_ELEMENT)
    expected_in_id = _player_id_by_element(db_session, season_id, _EXPECTED_ONE_TRANSFER_IN_ELEMENT)
    assert moves1[0].move_index == 0
    assert moves1[0].player_out_id == expected_out_id
    assert moves1[0].player_in_id == expected_in_id
    assert moves1[0].player_out_selling_price_snapshot == 54
    assert moves1[0].player_in_cost_snapshot == 42
    assert float(moves1[0].projected_points_gain_over_horizon) == pytest.approx(_EXPECTED_ONE_TRANSFER_GAIN)

    # 2-transfer scenario has exactly the two hand-verified best moves, in order.
    moves2 = (
        db_session.execute(
            select(TransferOptimizationRunMove)
            .where(TransferOptimizationRunMove.transfer_optimization_run_scenario_id == scenario2.id)
            .order_by(TransferOptimizationRunMove.move_index)
        )
        .scalars()
        .all()
    )
    assert len(moves2) == 2
    for move, (expected_out_element, expected_in_element, expected_gain) in zip(
        moves2, _EXPECTED_TWO_TRANSFER_MOVES, strict=True
    ):
        expected_out_id = _player_id_by_element(db_session, season_id, expected_out_element)
        expected_in_id = _player_id_by_element(db_session, season_id, expected_in_element)
        assert move.player_out_id == expected_out_id
        assert move.player_in_id == expected_in_id
        assert float(move.projected_points_gain_over_horizon) == pytest.approx(expected_gain)

    # Independent re-derivation of the recommended scenario's legality --
    # never trust the summary alone (budget + joint team-limit).
    constraints = load_optimization_constraints(db_session, season_id)
    total_in_cost = sum(m.player_in_cost_snapshot for m in moves2)
    total_out_selling = sum(m.player_out_selling_price_snapshot for m in moves2)
    assert total_in_cost <= total_out_selling + run_row.bank_snapshot

    squad_player_ids = [uuid.UUID(pid) for pid in snapshot.squad_player_ids]
    squad_players = {
        p.id: p for p in db_session.execute(select(Player).where(Player.id.in_(squad_player_ids))).scalars()
    }
    team_counts: dict[uuid.UUID, int] = {}
    for player in squad_players.values():
        team_counts[player.team_id] = team_counts.get(player.team_id, 0) + 1
    out_ids = {m.player_out_id for m in moves2}
    in_ids = {m.player_in_id for m in moves2}
    in_players = {p.id: p for p in db_session.execute(select(Player).where(Player.id.in_(in_ids))).scalars()}
    for out_id in out_ids:
        team_counts[squad_players[out_id].team_id] -= 1
    for in_id in in_ids:
        team_counts[in_players[in_id].team_id] = team_counts.get(in_players[in_id].team_id, 0) + 1
    assert all(count <= constraints.team_limit for count in team_counts.values())


def test_transfer_optimization_degrades_gracefully_with_unresolved_squad_player(
    db_session, patched_fpl_client_optimization_with_active_gameweek, patched_fpl_client_manager
):
    """A MANAGER_SYNC snapshot with an unresolved squad element (14 of 15 resolve) should still
    run TRANSFER_OPTIMIZATION successfully, with only the resolved 14 ever eligible as OUT moves.
    """
    _seed_players_with_active_gameweek(db_session)

    sync_job = execute_job(
        db_session,
        job_type=JobType.MANAGER_SYNC,
        parameters={"fpl_manager_id": 1},
        trigger_type=TriggerType.LOCAL,
    )
    assert sync_job.status == JobStatus.SUCCEEDED
    assert sync_job.output_json["has_unresolved_players"] is True
    snapshot_id = uuid.UUID(sync_job.output_json["manager_squad_snapshot_id"])
    snapshot = db_session.get(ManagerSquadSnapshot, snapshot_id)
    assert len(snapshot.squad_player_ids) == 14

    job = _run_transfer_optimization(db_session, snapshot_id)

    assert job.status == JobStatus.SUCCEEDED
    run_id = uuid.UUID(job.output_json["transfer_optimization_run_id"])

    scenario_rows = (
        db_session.execute(
            select(TransferOptimizationRunScenario).where(
                TransferOptimizationRunScenario.transfer_optimization_run_id == run_id
            )
        )
        .scalars()
        .all()
    )
    assert len(scenario_rows) == 3

    resolved_player_ids = {uuid.UUID(pid) for pid in snapshot.squad_player_ids}
    for scenario in scenario_rows:
        moves = (
            db_session.execute(
                select(TransferOptimizationRunMove).where(
                    TransferOptimizationRunMove.transfer_optimization_run_scenario_id == scenario.id
                )
            )
            .scalars()
            .all()
        )
        for move in moves:
            assert move.player_out_id in resolved_player_ids


def test_transfer_optimization_refused_when_optimization_not_permitted_creates_no_row(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    season_id = _seed_players_with_active_gameweek(db_session)
    snapshot = _create_manual_snapshot(db_session, season_id)

    ingestion_run = (
        db_session.execute(select(IngestionRun).order_by(IngestionRun.completed_at.desc())).scalars().first()
    )
    ingestion_run.optimization_permitted = False
    db_session.commit()

    before = len(db_session.execute(select(TransferOptimizationRun.id)).scalars().all())

    job = _run_transfer_optimization(db_session, snapshot.id)

    assert job.status == JobStatus.FAILED
    assert job.error_code == "OPTIMIZATION_NOT_PERMITTED"
    after = len(db_session.execute(select(TransferOptimizationRun.id)).scalars().all())
    assert after == before


def test_transfer_optimization_refused_when_rules_blocked_creates_no_row(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    season_id = _seed_players_with_active_gameweek(db_session)
    snapshot = _create_manual_snapshot(db_session, season_id)

    current_rules_config = db_session.execute(
        select(RulesConfig).where(RulesConfig.season_id == season_id, RulesConfig.is_current.is_(True))
    ).scalar_one()
    current_rules_config.rules_status = RulesStatus.BLOCKED
    db_session.commit()

    before = len(db_session.execute(select(TransferOptimizationRun.id)).scalars().all())

    job = _run_transfer_optimization(db_session, snapshot.id)

    assert job.status == JobStatus.FAILED
    assert job.error_code == "RULES_BLOCKED"
    after = len(db_session.execute(select(TransferOptimizationRun.id)).scalars().all())
    assert after == before


def test_transfer_optimization_refused_off_season_creates_no_row(db_session, patched_fpl_client_optimization):
    """The plain (all-finished) optimization fixture has no unfinished gameweek at all."""
    ingestion_job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert ingestion_job.status == JobStatus.SUCCEEDED
    season_id = db_session.execute(
        select(Season.id).where(Season.label == ingestion_job.output_json["season_label"])
    ).scalar_one()
    snapshot = _create_manual_snapshot(db_session, season_id)

    before = len(db_session.execute(select(TransferOptimizationRun.id)).scalars().all())

    job = _run_transfer_optimization(db_session, snapshot.id)

    assert job.status == JobStatus.FAILED
    assert job.error_code == "NO_ACTIVE_GAMEWEEK"
    after = len(db_session.execute(select(TransferOptimizationRun.id)).scalars().all())
    assert after == before


def test_transfer_optimization_snapshot_not_found(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    _seed_players_with_active_gameweek(db_session)

    job = _run_transfer_optimization(db_session, uuid.uuid4())

    assert job.status == JobStatus.FAILED
    assert job.error_code == "SNAPSHOT_NOT_FOUND"


def test_transfer_optimization_missing_snapshot_id(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    _seed_players_with_active_gameweek(db_session)

    job = execute_job(
        db_session,
        job_type=JobType.TRANSFER_OPTIMIZATION,
        parameters={},
        trigger_type=TriggerType.LOCAL,
    )

    assert job.status == JobStatus.FAILED
    assert job.error_code == "MISSING_SNAPSHOT_ID"
