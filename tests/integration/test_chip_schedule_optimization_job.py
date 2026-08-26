"""Integration tests for the CHIP_SCHEDULE_OPTIMIZATION job (Phase 1 of the joint chip scheduler)
against a real database.

Real bug this job fixes, and the thing every test here ultimately proves: ``CHIP_SIMULATION``
evaluates each of the 4 chips completely independently and can (did, live) recommend "use now" for
several chips in the SAME gameweek -- impossible to act on, since a real FPL manager can only play
one chip per gameweek. This job consumes an existing SUCCEEDED ``CHIP_SIMULATION`` run's own
``chip_window_candidates`` (unchanged, no re-evaluation) and picks the best non-colliding
gameweek-to-chip assignment.

Most tests here hand-seed ``ChipOptimizationRun``/``ChipWindowCandidate`` rows directly via the
ORM rather than running a real (expensive) ``CHIP_SIMULATION`` first -- this job only ever reads
those two tables, so hand-seeding gives full, deterministic control over the exact gain values
needed to prove collision-resolution correctness, without needing to reverse-engineer what real
chip evaluation would produce. One happy-path test (``test_..._real_chip_simulation_source_...``)
still runs a genuine end-to-end ``CHIP_SIMULATION`` -> ``CHIP_SCHEDULE_OPTIMIZATION`` chain, reusing
``test_chip_simulation_job.py``'s own fixture (duplicated, not imported, per this repo's own
established "duplicate test setup across test modules" convention -- see e.g.
``test_api_learning.py``'s own module docstring) -- that fixture's only unfinished gameweek (event
38) is EVERY chip's only real candidate, which conveniently makes the real, live collision this
whole feature exists to resolve trivial to reproduce with entirely real data.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select, update

from app.jobs.service import execute_job
from packages.database.models.chip_optimization_run import ChipOptimizationRun
from packages.database.models.chip_schedule_assignment import ChipScheduleAssignment
from packages.database.models.chip_schedule_optimization_run import ChipScheduleOptimizationRun
from packages.database.models.chip_window_candidate import ChipWindowCandidate
from packages.database.models.manager_squad_snapshot import ManagerSquadSnapshot
from packages.database.models.player import Player
from packages.database.models.rules_config import RulesConfig
from packages.database.models.season import Season
from packages.domain.chips import ChipOptimizationRunStatus, ChipScheduleRunStatus, ChipType
from packages.domain.jobs import JobStatus, JobType, TriggerType
from packages.domain.optimization import SolverStatus
from packages.domain.rules import RulesStatus
from packages.domain.transfers import ManagerSquadSnapshotSource

_STARTING_ELEMENT_IDS = [30001, 30010, 30011, 30019, 30014, 30028, 30029, 30030, 30031, 30048, 30049]
_BENCH_ELEMENT_IDS = [30002, 30016, 30032, 30050]
_SQUAD_ELEMENT_IDS = _STARTING_ELEMENT_IDS + _BENCH_ELEMENT_IDS
_CAPTAIN_ELEMENT_ID = 30048
_BANK = 20
_FREE_TRANSFERS_AVAILABLE = 1


def _seed_players_with_active_gameweek(db_session):
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
        raw_picks_json=_raw_picks_json(),
        raw_transfers_json=None,
        is_current=False,
        superseded_at=None,
    )
    db_session.add(snapshot)
    db_session.commit()
    db_session.refresh(snapshot)
    return snapshot


def _seed_chip_optimization_run(
    db_session,
    snapshot: ManagerSquadSnapshot,
    run_status: ChipOptimizationRunStatus = ChipOptimizationRunStatus.SUCCEEDED,
) -> ChipOptimizationRun:
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
        run_status=run_status,
        target_fpl_event_id=38,
        projection_formula_version=1,
        rules_status_snapshot=RulesStatus.OK,
        optimization_permitted_snapshot=True,
        data_quality_score_snapshot=None,
        parameters_json=None,
        started_at=now,
        completed_at=now if run_status != ChipOptimizationRunStatus.RUNNING else None,
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    return run


def _seed_candidate(
    db_session, run_id, chip_type: ChipType, window_number: int, fpl_event_id: int, gain_mean: float
) -> ChipWindowCandidate:
    candidate = ChipWindowCandidate(
        chip_optimization_run_id=run_id,
        chip_type=chip_type,
        window_number=window_number,
        candidate_fpl_event_id=fpl_event_id,
        is_blank_gameweek=None,
        is_double_gameweek=None,
        gain_mean=Decimal(str(gain_mean)),
        gain_floor=Decimal(str(gain_mean)),
        gain_ceiling=Decimal(str(gain_mean)),
        sample_count=None,
        rank_within_chip_type=1,
    )
    db_session.add(candidate)
    db_session.commit()
    db_session.refresh(candidate)
    return candidate


def _run_chip_schedule_optimization(db_session, **parameters):
    return execute_job(
        db_session,
        job_type=JobType.CHIP_SCHEDULE_OPTIMIZATION,
        parameters=parameters,
        requested_by="integration-test",
        trigger_type=TriggerType.LOCAL,
    )


def _assignments_by_chip_type(db_session, run_id) -> dict[ChipType, ChipScheduleAssignment]:
    rows = (
        db_session.execute(
            select(ChipScheduleAssignment).where(
                ChipScheduleAssignment.chip_schedule_optimization_run_id == run_id
            )
        )
        .scalars()
        .all()
    )
    return {row.chip_type: row for row in rows}


def test_chip_schedule_optimization_never_assigns_two_chips_to_the_same_gameweek(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    """The real, live bug: freehit/bboost/3xc all independently recommending GW2 -- reproduced here
    with hand-seeded candidates that all collide on the same gameweek."""
    season_id = _seed_players_with_active_gameweek(db_session)
    snapshot = _create_manual_snapshot(db_session, season_id)
    source_run = _seed_chip_optimization_run(db_session, snapshot)
    _seed_candidate(db_session, source_run.id, ChipType.FREEHIT, 1, 2, 3.3)
    _seed_candidate(db_session, source_run.id, ChipType.BBOOST, 1, 2, 5.1)
    _seed_candidate(db_session, source_run.id, ChipType.TRIPLE_CAPTAIN, 1, 2, 7.3)

    job = _run_chip_schedule_optimization(db_session, chip_optimization_run_id=str(source_run.id))

    assert job.status == JobStatus.SUCCEEDED, job.error_message
    run_id = uuid.UUID(job.output_json["chip_schedule_optimization_run_id"])
    run_row = db_session.get(ChipScheduleOptimizationRun, run_id)
    assert run_row is not None
    assert run_row.run_status == ChipScheduleRunStatus.SUCCEEDED
    assert run_row.solver_status == SolverStatus.OPTIMAL
    assert run_row.source_chip_optimization_run_id == source_run.id

    assignments = _assignments_by_chip_type(db_session, run_id)
    assigned = [a for a in assignments.values() if a.assigned_fpl_event_id is not None]
    assert len(assigned) == 1
    assert assigned[0].chip_type == ChipType.TRIPLE_CAPTAIN
    assert assigned[0].assigned_fpl_event_id == 2
    assert float(assigned[0].gain_mean) == 7.3
    assert float(run_row.total_expected_gain) == 7.3


def test_chip_schedule_optimization_auto_resolves_the_most_recent_succeeded_source_run(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    season_id = _seed_players_with_active_gameweek(db_session)
    snapshot = _create_manual_snapshot(db_session, season_id)
    source_run = _seed_chip_optimization_run(db_session, snapshot)
    _seed_candidate(db_session, source_run.id, ChipType.WILDCARD, 1, 4, 12.0)

    job = _run_chip_schedule_optimization(db_session)

    assert job.status == JobStatus.SUCCEEDED, job.error_message
    assert job.output_json["source_chip_optimization_run_id"] == str(source_run.id)


def test_chip_schedule_optimization_zero_candidates_succeeds_with_empty_assignments(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    """Every chip already used this season, or the source run evaluated nothing -- a real, valid
    outcome ("nothing left to schedule"), never a refusal."""
    season_id = _seed_players_with_active_gameweek(db_session)
    snapshot = _create_manual_snapshot(db_session, season_id)
    source_run = _seed_chip_optimization_run(db_session, snapshot)

    job = _run_chip_schedule_optimization(db_session, chip_optimization_run_id=str(source_run.id))

    assert job.status == JobStatus.SUCCEEDED, job.error_message
    run_id = uuid.UUID(job.output_json["chip_schedule_optimization_run_id"])
    run_row = db_session.get(ChipScheduleOptimizationRun, run_id)
    assert run_row.run_status == ChipScheduleRunStatus.SUCCEEDED
    assert float(run_row.total_expected_gain) == 0.0
    assert _assignments_by_chip_type(db_session, run_id) == {}


def test_chip_schedule_optimization_missing_source_run_id_is_rejected(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    missing_id = uuid.uuid4()

    job = _run_chip_schedule_optimization(db_session, chip_optimization_run_id=str(missing_id))

    assert job.status == JobStatus.FAILED
    assert job.error_code == "CHIP_OPTIMIZATION_RUN_NOT_FOUND"


def test_chip_schedule_optimization_invalid_source_run_id_is_rejected(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    job = _run_chip_schedule_optimization(db_session, chip_optimization_run_id="not-a-uuid")

    assert job.status == JobStatus.FAILED
    assert job.error_code == "INVALID_CHIP_OPTIMIZATION_RUN_ID"


def test_chip_schedule_optimization_refuses_a_non_succeeded_source_run(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    season_id = _seed_players_with_active_gameweek(db_session)
    snapshot = _create_manual_snapshot(db_session, season_id)
    source_run = _seed_chip_optimization_run(
        db_session, snapshot, run_status=ChipOptimizationRunStatus.FAILED
    )

    job = _run_chip_schedule_optimization(db_session, chip_optimization_run_id=str(source_run.id))

    assert job.status == JobStatus.FAILED
    assert job.error_code == "CHIP_OPTIMIZATION_RUN_NOT_SUCCEEDED"


def test_chip_schedule_optimization_no_succeeded_source_run_anywhere_is_refused(db_session):
    """NO_SUCCEEDED_CHIP_SIMULATION_RUN can only be proven by there being none ANYWHERE -- a
    genuinely global, un-scoped query, unlike every other test in this module. Flips every
    existing row's run_status away from SUCCEEDED (an UPDATE, not a DELETE -- other tables, e.g.
    recommendation_audits, hold real FK references to these rows from earlier test modules in
    this same shared, non-truncated Postgres, so deleting them is not an option) rather than
    trying to fabricate a genuinely empty table. Mirrors this repo's own established "blanket
    reset for a genuinely global query" precedent (e.g. test_api_models.py's
    ``_clear_champion_flag``) -- accepted here for the same reason. Real rows' own run_status
    changing under them is a real, if narrow, side effect of this specific test, same as that
    established precedent already accepts elsewhere.
    """
    db_session.execute(
        update(ChipOptimizationRun)
        .where(ChipOptimizationRun.run_status == ChipOptimizationRunStatus.SUCCEEDED)
        .values(run_status=ChipOptimizationRunStatus.FAILED)
    )
    db_session.commit()

    job = _run_chip_schedule_optimization(db_session)

    assert job.status == JobStatus.FAILED
    assert job.error_code == "NO_SUCCEEDED_CHIP_SIMULATION_RUN"


def test_chip_schedule_optimization_real_chip_simulation_source_still_resolves_a_single_winner(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    """Genuine end-to-end: a real CHIP_SIMULATION run (not hand-seeded) feeds a real
    CHIP_SCHEDULE_OPTIMIZATION run. This fixture's only unfinished gameweek (event 38) is every
    chip's only real candidate -- so if 2+ chips came back with a positive-gain candidate, this
    proves the solver picks exactly one for event 38 and leaves the rest unassigned, using
    entirely real evaluation output, not synthetic data."""
    season_id = _seed_players_with_active_gameweek(db_session)
    snapshot = _create_manual_snapshot(db_session, season_id)
    simulation_job = execute_job(
        db_session,
        job_type=JobType.CHIP_SIMULATION,
        parameters={"manager_squad_snapshot_id": str(snapshot.id)},
        requested_by="integration-test",
        trigger_type=TriggerType.LOCAL,
    )
    assert simulation_job.status == JobStatus.SUCCEEDED
    source_run_id = uuid.UUID(simulation_job.output_json["chip_optimization_run_id"])

    job = _run_chip_schedule_optimization(db_session, chip_optimization_run_id=str(source_run_id))

    assert job.status == JobStatus.SUCCEEDED, job.error_message
    run_id = uuid.UUID(job.output_json["chip_schedule_optimization_run_id"])
    assignments = _assignments_by_chip_type(db_session, run_id)
    assigned = [a for a in assignments.values() if a.assigned_fpl_event_id is not None]
    # Every candidate across every chip is event 38 here (the fixture's only unfinished gameweek)
    # -- mutual exclusivity means at most one chip can win it.
    assert len(assigned) <= 1
    for assignment in assigned:
        assert assignment.assigned_fpl_event_id == 38
