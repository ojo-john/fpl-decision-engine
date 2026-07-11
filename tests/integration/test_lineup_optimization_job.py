"""Integration tests for the LINEUP_OPTIMIZATION job end-to-end through execute_job.

Covers both squad-source input modes: chaining off a prior SUCCEEDED
SQUAD_OPTIMIZATION run (``source_optimization_run_id``), and an explicit
``player_ids`` list (the only realistic path until manager-squad sync exists
in a later milestone).
"""

import uuid
from collections import Counter

from sqlalchemy import select

from app.jobs.service import execute_job
from packages.database.models.optimization_run import OptimizationRun
from packages.database.models.optimization_run_player import OptimizationRunPlayer
from packages.database.models.player import Player
from packages.database.models.position import Position
from packages.domain.jobs import JobStatus, JobType, TriggerType
from packages.domain.optimization import OptimizationRunStatus, RecommendationType, SolverStatus

EXPECTED_STARTING_POSITION_MINIMUMS = {"GKP": 1, "DEF": 3, "MID": 2, "FWD": 1}


def _run_squad_job(db_session):
    job = execute_job(db_session, job_type=JobType.SQUAD_OPTIMIZATION, trigger_type=TriggerType.LOCAL)
    assert job.status == JobStatus.SUCCEEDED
    return job


def _assert_legal_lineup(db_session, optimization_run_id, expected_squad_player_ids):
    run_row = db_session.get(OptimizationRun, optimization_run_id)
    assert run_row is not None
    assert run_row.recommendation_type == RecommendationType.STARTING_XI
    assert run_row.run_status == OptimizationRunStatus.SUCCEEDED
    assert run_row.solver_status == SolverStatus.OPTIMAL
    assert run_row.captain_player_id is not None
    assert run_row.vice_captain_player_id is not None
    assert run_row.captain_player_id != run_row.vice_captain_player_id

    run_players = (
        db_session.execute(
            select(OptimizationRunPlayer).where(
                OptimizationRunPlayer.optimization_run_id == optimization_run_id
            )
        )
        .scalars()
        .all()
    )
    assert len(run_players) == 15
    assert {rp.player_id for rp in run_players} == set(expected_squad_player_ids)

    starters = [rp for rp in run_players if rp.is_starting]
    bench = [rp for rp in run_players if not rp.is_starting]
    assert len(starters) == 11
    assert len(bench) == 4

    # Bench order: exactly 1-4, unique, and each row's bench_order lines up
    # with a starting=False row (no starter ever carries a bench_order).
    assert sorted(rp.bench_order for rp in bench) == [1, 2, 3, 4]
    assert all(rp.bench_order is None for rp in starters)

    # Bench is in descending-projection order.
    bench_by_order = sorted(bench, key=lambda rp: rp.bench_order)
    for earlier, later in zip(bench_by_order, bench_by_order[1:], strict=False):
        assert float(earlier.projected_points) >= float(later.projected_points)

    # Captain/vice-captain are distinct starters, and are exactly the top-2
    # projected starters (ties broken however solve_lineup's rank_key does,
    # but captain must never be projected below vice, and both are starters).
    captain_rows = [rp for rp in starters if rp.is_captain]
    vice_rows = [rp for rp in starters if rp.is_vice_captain]
    assert len(captain_rows) == 1
    assert len(vice_rows) == 1
    assert captain_rows[0].player_id == run_row.captain_player_id
    assert vice_rows[0].player_id == run_row.vice_captain_player_id

    starters_by_points_desc = sorted(starters, key=lambda rp: -float(rp.projected_points))
    top_two_ids = {starters_by_points_desc[0].player_id, starters_by_points_desc[1].player_id}
    assert {run_row.captain_player_id, run_row.vice_captain_player_id} == top_two_ids

    # Legal per-position starting counts.
    player_rows = {
        p.id: p
        for p in db_session.execute(
            select(Player).where(Player.id.in_([rp.player_id for rp in starters]))
        ).scalars()
    }
    position_ids = {p.position_id for p in player_rows.values()}
    positions_by_id = {
        p.id: p for p in db_session.execute(select(Position).where(Position.id.in_(position_ids))).scalars()
    }
    starting_position_counts = Counter(
        positions_by_id[player_rows[rp.player_id].position_id].short_name for rp in starters
    )
    for short_name, minimum in EXPECTED_STARTING_POSITION_MINIMUMS.items():
        assert starting_position_counts.get(short_name, 0) >= minimum
    assert sum(starting_position_counts.values()) == 11

    return run_players


def test_lineup_optimization_chained_from_squad_run(db_session, patched_fpl_client_optimization):
    ingestion_job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert ingestion_job.status == JobStatus.SUCCEEDED

    squad_job = _run_squad_job(db_session)
    squad_run_id = uuid.UUID(squad_job.output_json["optimization_run_id"])
    expected_squad_ids = [uuid.UUID(pid) for pid in squad_job.output_json["squad_player_ids"]]

    lineup_job = execute_job(
        db_session,
        job_type=JobType.LINEUP_OPTIMIZATION,
        parameters={"source_optimization_run_id": str(squad_run_id)},
        trigger_type=TriggerType.LOCAL,
    )
    assert lineup_job.status == JobStatus.SUCCEEDED
    lineup_run_id = uuid.UUID(lineup_job.output_json["optimization_run_id"])

    run_row = db_session.get(OptimizationRun, lineup_run_id)
    assert run_row.source_optimization_run_id == squad_run_id

    _assert_legal_lineup(db_session, lineup_run_id, expected_squad_ids)


def test_lineup_optimization_explicit_player_ids(db_session, patched_fpl_client_optimization):
    ingestion_job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert ingestion_job.status == JobStatus.SUCCEEDED

    squad_job = _run_squad_job(db_session)
    squad_player_ids = squad_job.output_json["squad_player_ids"]

    lineup_job = execute_job(
        db_session,
        job_type=JobType.LINEUP_OPTIMIZATION,
        parameters={"player_ids": squad_player_ids},
        trigger_type=TriggerType.LOCAL,
    )
    assert lineup_job.status == JobStatus.SUCCEEDED
    lineup_run_id = uuid.UUID(lineup_job.output_json["optimization_run_id"])

    run_row = db_session.get(OptimizationRun, lineup_run_id)
    assert run_row.source_optimization_run_id is None  # explicit player_ids mode, no chained source

    _assert_legal_lineup(db_session, lineup_run_id, [uuid.UUID(pid) for pid in squad_player_ids])


def test_lineup_optimization_rejects_ambiguous_input(db_session, patched_fpl_client_optimization):
    ingestion_job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert ingestion_job.status == JobStatus.SUCCEEDED

    squad_job = _run_squad_job(db_session)
    squad_run_id = squad_job.output_json["optimization_run_id"]
    squad_player_ids = squad_job.output_json["squad_player_ids"]

    before_count = db_session.execute(select(OptimizationRun.id)).scalars().all()

    job = execute_job(
        db_session,
        job_type=JobType.LINEUP_OPTIMIZATION,
        parameters={"source_optimization_run_id": squad_run_id, "player_ids": squad_player_ids},
        trigger_type=TriggerType.LOCAL,
    )
    assert job.status == JobStatus.FAILED
    assert job.error_code == "INVALID_LINEUP_INPUT"

    after_count = db_session.execute(select(OptimizationRun.id)).scalars().all()
    assert len(after_count) == len(before_count)  # no new optimization_runs row created
