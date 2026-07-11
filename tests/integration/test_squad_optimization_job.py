"""Integration tests for the SQUAD_OPTIMIZATION job end-to-end through execute_job.

Requires a running Postgres with migrations applied (see conftest.py).
FPLClient is monkeypatched with the ~60-player synthetic candidate pool (see
`patched_fpl_client_optimization` in conftest.py) so no real network access
happens here, and both solvers always have a comfortably-solvable pool.
"""

import uuid
from collections import Counter

import pytest
from sqlalchemy import select

from app.jobs.service import execute_job
from packages.database.models.optimization_run import OptimizationRun
from packages.database.models.optimization_run_player import OptimizationRunPlayer
from packages.database.models.player import Player
from packages.database.models.position import Position
from packages.database.models.season import Season
from packages.domain.jobs import JobStatus, JobType, TriggerType
from packages.domain.optimization import (
    OptimizationRunStatus,
    RecommendationType,
    SolverStatus,
    TargetGameweekStatus,
)

EXPECTED_POSITION_COUNTS = {"GKP": 2, "DEF": 5, "MID": 5, "FWD": 3}
TOTAL_SPEND_LIMIT = 1000  # GBP100.0m, in now_cost's tenths-of-a-million unit
TEAM_LIMIT = 3


def _season_id_for_label(db_session, label: str):
    return db_session.execute(select(Season.id).where(Season.label == label)).scalar_one()


def test_squad_optimization_job_produces_legal_squad(db_session, patched_fpl_client_optimization):
    ingestion_job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert ingestion_job.status == JobStatus.SUCCEEDED
    season_label = ingestion_job.output_json["season_label"]
    season_id = _season_id_for_label(db_session, season_label)

    squad_job = execute_job(db_session, job_type=JobType.SQUAD_OPTIMIZATION, trigger_type=TriggerType.LOCAL)

    assert squad_job.status == JobStatus.SUCCEEDED
    output = squad_job.output_json
    optimization_run_id = uuid.UUID(output["optimization_run_id"])
    assert len(output["squad_player_ids"]) == 15
    assert output["target_gameweek_status"] == TargetGameweekStatus.NO_ACTIVE_GAMEWEEK.value

    run_row = db_session.get(OptimizationRun, optimization_run_id)
    assert run_row is not None
    assert run_row.season_id == season_id
    assert run_row.recommendation_type == RecommendationType.JOINT_SQUAD_AND_XI
    assert run_row.run_status == OptimizationRunStatus.SUCCEEDED
    assert run_row.solver_status == SolverStatus.OPTIMAL
    assert run_row.squad_total_cost is not None
    assert run_row.squad_projected_points is not None
    assert run_row.xi_projected_points is not None
    assert run_row.captain_player_id is not None
    assert run_row.vice_captain_player_id is not None
    assert run_row.captain_player_id != run_row.vice_captain_player_id

    # Never trust the returned summary alone — re-derive everything from the
    # persisted optimization_run_players rows (joined to players/positions).
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
    assert {rp.player_id for rp in run_players} == {uuid.UUID(pid) for pid in output["squad_player_ids"]}

    # Budget: sum(now_cost_snapshot) must not exceed GBP100.0m and must match
    # the header row's squad_total_cost exactly.
    total_cost = sum(rp.now_cost_snapshot for rp in run_players)
    assert total_cost <= TOTAL_SPEND_LIMIT
    assert total_cost == run_row.squad_total_cost

    total_projected = sum(float(rp.projected_points) for rp in run_players)
    assert total_projected == pytest.approx(float(run_row.squad_projected_points))

    player_rows = {
        p.id: p
        for p in db_session.execute(
            select(Player).where(Player.id.in_([rp.player_id for rp in run_players]))
        ).scalars()
    }
    positions_by_id = {
        p.id: p for p in db_session.execute(select(Position).where(Position.season_id == season_id)).scalars()
    }

    # Per-position squad-select counts (2 GKP / 5 DEF / 5 MID / 3 FWD).
    position_counts = Counter(
        positions_by_id[player_rows[rp.player_id].position_id].short_name for rp in run_players
    )
    assert dict(position_counts) == EXPECTED_POSITION_COUNTS

    # Team limit: at most 3 players from any single club.
    team_counts = Counter(player_rows[rp.player_id].team_id for rp in run_players)
    assert all(count <= TEAM_LIMIT for count in team_counts.values())

    # No quarantined or unavailable ('i'/'s'/'u') player should ever be picked.
    for rp in run_players:
        player = player_rows[rp.player_id]
        assert player.is_quarantined is False
        assert player.status not in {"i", "s", "u"}

    # Fix 2: the joint solver also resolves a full starting XI, ordered bench
    # (real FPL slot numbers 12-15), and captain/vice in the same run.
    starters = [rp for rp in run_players if rp.is_starting]
    bench = [rp for rp in run_players if not rp.is_starting]
    assert len(starters) == 11
    assert len(bench) == 4
    assert all(rp.bench_order is None for rp in starters)
    assert sorted(rp.bench_order for rp in bench) == [12, 13, 14, 15]

    captain_rows = [rp for rp in run_players if rp.is_captain]
    vice_rows = [rp for rp in run_players if rp.is_vice_captain]
    assert len(captain_rows) == 1
    assert len(vice_rows) == 1
    assert captain_rows[0].player_id != vice_rows[0].player_id
    assert captain_rows[0].is_starting
    assert vice_rows[0].is_starting
    assert captain_rows[0].player_id == run_row.captain_player_id
    assert vice_rows[0].player_id == run_row.vice_captain_player_id
