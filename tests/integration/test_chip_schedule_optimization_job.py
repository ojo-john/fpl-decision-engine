"""Integration tests for the CHIP_SCHEDULE_OPTIMIZATION job (the joint chip scheduler) against a
real database.

Phase 2 real structural change from Phase 1: this handler no longer reads an existing
``CHIP_SIMULATION`` run at all -- it takes a ``manager_squad_snapshot_id`` directly and evaluates
fresh, both windows, all 4 chips, with real path-dependent re-evaluation for anything after a
Wildcard candidate. These tests exercise the REAL wiring (context resolution, gate check,
both-window evaluation, real ``solve_joint_squad_and_xi``-backed post-Wildcard variants, the
solver) end to end against real seeded data -- the pure ALGORITHMIC proof that path dependence
actually changes the answer already lives in ``tests/unit/test_chip_schedule_solver.py`` (fully
controlled synthetic gains); this module's own job is proving the real pieces compose correctly,
mirroring this repo's own established split between "does the building block work" (covered
elsewhere) and "is it wired up correctly here".

Reuses the same real fixture/squad shape ``test_chip_simulation_job.py`` already established
(duplicated, not imported -- this repo's own "duplicate test setup across test modules"
convention) -- real Wildcard/Free Hit squad rebuilds need this exact legally-solvable player pool.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select, update

from app.jobs.service import execute_job
from packages.database.models.chip_schedule_assignment import ChipScheduleAssignment
from packages.database.models.chip_schedule_optimization_run import ChipScheduleOptimizationRun
from packages.database.models.gameweek import Gameweek
from packages.database.models.manager_squad_snapshot import ManagerSquadSnapshot
from packages.database.models.player import Player
from packages.database.models.season import Season
from packages.domain.chips import ChipScheduleRunStatus, ChipType
from packages.domain.jobs import JobStatus, JobType, TriggerType
from packages.domain.optimization import SolverStatus
from packages.domain.transfers import ManagerSquadSnapshotSource


@pytest.fixture(autouse=True)
def _cleanup_chip_schedule_optimization_runs(db_session):
    """Every row this module creates has ``source_chip_optimization_run_id=NULL`` by design (see
    module docstring) -- real, found-live fallout: ``tests/integration/test_migrations.py``'s own
    full ``alembic downgrade base`` run re-imposes migration ``0048``'s ``NOT NULL`` constraint on
    that exact column, which fails against any real NULL row left behind on this shared,
    non-truncated Postgres. Blanket delete (``ChipScheduleAssignment`` cascades via
    ``ondelete="CASCADE"``) before AND after every test here -- mirrors this repo's own established
    ``_clear_champion_flag``-style precedent for this exact cross-test-pollution risk; nothing else
    in this schema has an FK pointing AT ``chip_schedule_optimization_runs``, so this is safe.
    """
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


def _mark_gameweeks_actionable(db_session, season_id, fpl_event_ids: list[int]) -> None:
    """Real, ascending future deadlines for specific gameweeks (by fpl_event_id) -- mirrors
    test_chip_simulation_job.py's own _mark_trailing_gameweeks_unfinished, generalized to
    caller-chosen event ids (needed here to seed candidates spanning BOTH chip windows, not just
    the fixture's usual single trailing one)."""
    now = datetime.now(UTC)
    for offset, fpl_event_id in enumerate(sorted(fpl_event_ids)):
        db_session.execute(
            update(Gameweek)
            .where(Gameweek.season_id == season_id, Gameweek.fpl_event_id == fpl_event_id)
            .values(finished=False, deadline_time=now + timedelta(days=7 * (offset + 1)))
        )
    db_session.commit()


def _run_chip_schedule_optimization(db_session, **parameters):
    return execute_job(
        db_session,
        job_type=JobType.CHIP_SCHEDULE_OPTIMIZATION,
        parameters=parameters,
        requested_by="integration-test",
        trigger_type=TriggerType.LOCAL,
    )


def _assignments_by_slot(db_session, run_id) -> dict[tuple[ChipType, int], ChipScheduleAssignment]:
    rows = (
        db_session.execute(
            select(ChipScheduleAssignment).where(
                ChipScheduleAssignment.chip_schedule_optimization_run_id == run_id
            )
        )
        .scalars()
        .all()
    )
    return {(row.chip_type, row.window_number): row for row in rows}


def test_chip_schedule_optimization_real_end_to_end_evaluates_both_windows(
    db_session, patched_fpl_client_optimization
):
    """Marks one gameweek in window 1 (event 8) and one in window 2 (event 25) actionable -- proves
    the real handler evaluates BOTH windows now (Phase 1 only ever saw whichever one window a
    source CHIP_SIMULATION run happened to cover), using entirely real evaluation (real Monte
    Carlo, a real Wildcard squad rebuild), not synthetic data."""
    season_id = _seed_players_with_active_gameweek(db_session)
    _mark_gameweeks_actionable(db_session, season_id, [8, 25])
    snapshot = _create_manual_snapshot(db_session, season_id)

    job = _run_chip_schedule_optimization(db_session, manager_squad_snapshot_id=str(snapshot.id))

    assert job.status == JobStatus.SUCCEEDED, job.error_message
    run_id = uuid.UUID(job.output_json["chip_schedule_optimization_run_id"])
    run_row = db_session.get(ChipScheduleOptimizationRun, run_id)
    assert run_row is not None
    assert run_row.run_status == ChipScheduleRunStatus.SUCCEEDED
    assert run_row.solver_status == SolverStatus.OPTIMAL
    assert run_row.manager_squad_snapshot_id == snapshot.id
    # Phase 2 has no source CHIP_SIMULATION run -- always None (migration 0048).
    assert run_row.source_chip_optimization_run_id is None

    assignments = _assignments_by_slot(db_session, run_id)
    window_numbers_seen = {window_number for (_chip_type, window_number) in assignments}
    assert window_numbers_seen == {1, 2}

    # Never two chips on the same real gameweek -- the actual bug this whole feature exists to fix.
    assigned_events = [
        a.assigned_fpl_event_id for a in assignments.values() if a.assigned_fpl_event_id is not None
    ]
    assert len(assigned_events) == len(set(assigned_events))

    # Phase 2 candidates/variants are ephemeral -- no persisted chip_window_candidates row to trace.
    for assignment in assignments.values():
        assert assignment.source_chip_window_candidate_id is None


def test_chip_schedule_optimization_missing_snapshot_id_is_rejected(db_session):
    job = _run_chip_schedule_optimization(db_session)

    assert job.status == JobStatus.FAILED
    assert job.error_code == "MISSING_MANAGER_SQUAD_SNAPSHOT_ID"


def test_chip_schedule_optimization_unknown_snapshot_is_rejected(db_session):
    job = _run_chip_schedule_optimization(db_session, manager_squad_snapshot_id=str(uuid.uuid4()))

    assert job.status == JobStatus.FAILED
    assert job.error_code == "SNAPSHOT_NOT_FOUND"


def test_chip_schedule_optimization_off_season_is_rejected(db_session, patched_fpl_client_optimization):
    """The plain (non-active-gameweek) fixture has every gameweek finished AND deadline-passed --
    the real off-season state this whole codebase treats as NO_ACTIVE_GAMEWEEK, not an error."""
    season_id = _seed_players_with_active_gameweek(db_session)
    snapshot = _create_manual_snapshot(db_session, season_id)

    job = _run_chip_schedule_optimization(db_session, manager_squad_snapshot_id=str(snapshot.id))

    assert job.status == JobStatus.FAILED
    assert job.error_code == "NO_ACTIVE_GAMEWEEK"
    # A hard refusal before any row exists -- no chip_schedule_optimization_runs row created.
    assert job.output_json is None


def test_chip_schedule_optimization_zero_eligible_candidates_succeeds_with_empty_assignments(
    db_session, patched_fpl_client_optimization
):
    """Every chip already used this season -- a real, valid outcome ('nothing left to schedule'),
    never a refusal. Reuses the off-season fixture but marks ONE gameweek actionable, with a
    chips_used_json marking all 4 chips already used within that gameweek's own window."""
    season_id = _seed_players_with_active_gameweek(db_session)
    _mark_gameweeks_actionable(db_session, season_id, [25])
    gameweek_id = db_session.execute(
        select(Gameweek.id).where(Gameweek.season_id == season_id, Gameweek.fpl_event_id == 25)
    ).scalar_one()

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
        as_of_gameweek_id=gameweek_id,
        bank=_BANK,
        free_transfers_available=_FREE_TRANSFERS_AVAILABLE,
        active_chip=None,
        chips_used_json=[
            {"name": "wildcard", "event": 20},
            {"name": "freehit", "event": 20},
            {"name": "bboost", "event": 20},
            {"name": "3xc", "event": 20},
        ],
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

    job = _run_chip_schedule_optimization(db_session, manager_squad_snapshot_id=str(snapshot.id))

    assert job.status == JobStatus.SUCCEEDED, job.error_message
    run_id = uuid.UUID(job.output_json["chip_schedule_optimization_run_id"])
    run_row = db_session.get(ChipScheduleOptimizationRun, run_id)
    assert run_row.run_status == ChipScheduleRunStatus.SUCCEEDED
    assert float(run_row.total_expected_gain) == 0.0
    assert _assignments_by_slot(db_session, run_id) == {}
