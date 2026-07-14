"""Integration tests for the H2H_SIMULATION job against a real database.

Reuses ``patched_fpl_client_optimization_with_active_gameweek`` (the
~60-player optimization pool, one unfinished gameweek -- see conftest.py) to
seed current-season players, and ``patched_fpl_client_league`` (the
synthetic 4-entry H2H league -- see that fixture's own docstring) via a real
LEAGUE_SYNC run to produce a real, realistic ``RivalProfile`` row: entry B's
picks/history resolve cleanly (``last_known_snapshot_id`` set,
``overall_confidence=LOW`` -- the honest cold-start floor every real rival
starts at, see ``test_rival_profile.py``), entry C's picks are unavailable
(``last_known_snapshot_id=None``, exercising the ``RIVAL_SQUAD_UNAVAILABLE``
refusal path).

The user's own squad is a hand-built ``MANUAL_ENTRY`` ``ManagerSquadSnapshot``
with a real ``raw_picks_json`` (11 starters + 4 bench, one captain) drawn
from the same ~60-player pool -- mirrors
``test_transfer_optimization_job.py``'s own "hand-verifiable manual snapshot"
discipline, but this job additionally NEEDS ``raw_picks_json`` (not just
``squad_player_ids``) to resolve a starting XI/captain at all (see
``app.jobs.handlers.h2h_simulation``'s own docstring).
"""

import uuid

import pytest
from sqlalchemy import select

from app.jobs.service import execute_job
from packages.database.models.h2h_simulation_run import H2HSimulationRun
from packages.database.models.ingestion_run import IngestionRun
from packages.database.models.manager_squad_snapshot import ManagerSquadSnapshot
from packages.database.models.player import Player
from packages.database.models.rival_profile import RivalProfile
from packages.database.models.rules_config import RulesConfig
from packages.database.models.season import Season
from packages.domain.h2h import H2HSimulationRunStatus, RivalStateConfidence
from packages.domain.jobs import JobStatus, JobType, TriggerType
from packages.domain.rules import RulesStatus
from packages.domain.transfers import ManagerSquadSnapshotSource
from tests.integration.conftest import (
    LEAGUE_RIVAL_B_FPL_MANAGER_ID,
    LEAGUE_RIVAL_C_FPL_MANAGER_ID,
    LEAGUE_SELF_FPL_MANAGER_ID,
)

FPL_LEAGUE_ID = 88891  # distinct from test_league_sync_job.py's 88888/test_rival_profile.py's 88889

# 11 starters (one captained) + 4 bench, all drawn from the ~60-player
# synthetic optimization pool (element ids 30000-30059, see
# build_optimization_bootstrap_json in conftest.py) -- deliberately disjoint
# from the rival's own squad (picks_b.json's 30000/30010-13/30028-31/30048-50)
# so the two sides are visibly distinct squads.
_USER_STARTING_ELEMENTS = [30003, 30015, 30016, 30017, 30018, 30033, 30034, 30035, 30036, 30052, 30053]
_USER_CAPTAIN_ELEMENT = 30034
_USER_BENCH_ELEMENTS = [30004, 30019, 30038, 30054]


def _user_raw_picks_json() -> dict:
    picks = []
    for position, element in enumerate(_USER_STARTING_ELEMENTS, start=1):
        picks.append(
            {
                "element": element,
                "position": position,
                "multiplier": 2 if element == _USER_CAPTAIN_ELEMENT else 1,
                "is_captain": element == _USER_CAPTAIN_ELEMENT,
                "is_vice_captain": False,
            }
        )
    for offset, element in enumerate(_USER_BENCH_ELEMENTS):
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


def _seed_players_with_active_gameweek(db_session):
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


def _rival_profile(db_session, season_id, fpl_manager_id: int) -> RivalProfile:
    return db_session.execute(
        select(RivalProfile).where(
            RivalProfile.season_id == season_id, RivalProfile.fpl_manager_id == fpl_manager_id
        )
    ).scalar_one()


def _create_manual_user_snapshot(db_session, season_id, raw_picks_json: dict | None) -> ManagerSquadSnapshot:
    all_elements = _USER_STARTING_ELEMENTS + _USER_BENCH_ELEMENTS
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
        raw_picks_json=raw_picks_json,
        raw_transfers_json=None,
        is_current=False,
        superseded_at=None,
    )
    db_session.add(snapshot)
    db_session.commit()
    db_session.refresh(snapshot)
    return snapshot


def _run_h2h_simulation(db_session, snapshot_id, rival_profile_id, **extra_parameters):
    parameters = {
        "manager_squad_snapshot_id": str(snapshot_id),
        "rival_profile_id": str(rival_profile_id),
        **extra_parameters,
    }
    return execute_job(
        db_session,
        job_type=JobType.H2H_SIMULATION,
        parameters=parameters,
        requested_by="integration-test",
        trigger_type=TriggerType.LOCAL,
    )


def test_h2h_simulation_happy_path_produces_a_sane_succeeded_run(
    db_session, patched_fpl_client_optimization_with_active_gameweek, patched_fpl_client_league
):
    season_id = _seed_players_with_active_gameweek(db_session)
    league_job = _run_league_sync(db_session)
    assert league_job.status == JobStatus.SUCCEEDED

    rival_profile = _rival_profile(db_session, season_id, LEAGUE_RIVAL_B_FPL_MANAGER_ID)
    assert rival_profile.last_known_snapshot_id is not None
    assert rival_profile.overall_confidence == RivalStateConfidence.LOW  # realistic cold-start floor

    snapshot = _create_manual_user_snapshot(db_session, season_id, _user_raw_picks_json())

    job = _run_h2h_simulation(db_session, snapshot.id, rival_profile.id)

    assert job.status == JobStatus.SUCCEEDED
    output = job.output_json
    run_id = uuid.UUID(output["h2h_simulation_run_id"])
    run_row = db_session.get(H2HSimulationRun, run_id)
    assert run_row is not None
    assert run_row.run_status == H2HSimulationRunStatus.SUCCEEDED
    assert run_row.manager_squad_snapshot_id == snapshot.id
    assert run_row.rival_profile_id == rival_profile.id
    assert run_row.sample_count == 5000
    assert run_row.completed_at is not None

    # Confidence must never overclaim beyond the rival profile's own floor.
    assert run_row.confidence == RivalStateConfidence.LOW

    # Sane, non-negative scores and a coherent probability triple.
    assert run_row.user_score_mean >= 0
    assert run_row.rival_score_mean >= 0
    assert run_row.user_score_floor <= run_row.user_score_mean <= run_row.user_score_ceiling
    assert run_row.rival_score_floor <= run_row.rival_score_mean <= run_row.rival_score_ceiling
    total_probability = (
        float(run_row.win_probability) + float(run_row.draw_probability) + float(run_row.loss_probability)
    )
    assert total_probability == pytest.approx(1.0, abs=1e-6)
    assert 0 <= float(run_row.win_probability) <= 1
    assert 0 <= float(run_row.draw_probability) <= 1
    assert 0 <= float(run_row.loss_probability) <= 1

    assert output["win_probability"] == pytest.approx(float(run_row.win_probability))
    assert output["confidence"] == RivalStateConfidence.LOW.value


def test_h2h_simulation_accepts_an_explicit_target_fpl_event_id(
    db_session, patched_fpl_client_optimization_with_active_gameweek, patched_fpl_client_league
):
    season_id = _seed_players_with_active_gameweek(db_session)
    _run_league_sync(db_session)
    rival_profile = _rival_profile(db_session, season_id, LEAGUE_RIVAL_B_FPL_MANAGER_ID)
    snapshot = _create_manual_user_snapshot(db_session, season_id, _user_raw_picks_json())

    job = _run_h2h_simulation(db_session, snapshot.id, rival_profile.id, target_fpl_event_id=1)

    assert job.status == JobStatus.SUCCEEDED
    run_row = db_session.get(H2HSimulationRun, uuid.UUID(job.output_json["h2h_simulation_run_id"]))
    assert run_row.target_fpl_event_id == 1


def test_h2h_simulation_refused_when_optimization_not_permitted_creates_no_row(
    db_session, patched_fpl_client_optimization_with_active_gameweek, patched_fpl_client_league
):
    season_id = _seed_players_with_active_gameweek(db_session)
    _run_league_sync(db_session)
    rival_profile = _rival_profile(db_session, season_id, LEAGUE_RIVAL_B_FPL_MANAGER_ID)
    snapshot = _create_manual_user_snapshot(db_session, season_id, _user_raw_picks_json())

    ingestion_run = (
        db_session.execute(select(IngestionRun).order_by(IngestionRun.completed_at.desc())).scalars().first()
    )
    ingestion_run.optimization_permitted = False
    db_session.commit()

    before = len(db_session.execute(select(H2HSimulationRun.id)).scalars().all())

    job = _run_h2h_simulation(db_session, snapshot.id, rival_profile.id)

    assert job.status == JobStatus.FAILED
    assert job.error_code == "OPTIMIZATION_NOT_PERMITTED"
    after = len(db_session.execute(select(H2HSimulationRun.id)).scalars().all())
    assert after == before


def test_h2h_simulation_refused_when_rules_blocked_creates_no_row(
    db_session, patched_fpl_client_optimization_with_active_gameweek, patched_fpl_client_league
):
    season_id = _seed_players_with_active_gameweek(db_session)
    _run_league_sync(db_session)
    rival_profile = _rival_profile(db_session, season_id, LEAGUE_RIVAL_B_FPL_MANAGER_ID)
    snapshot = _create_manual_user_snapshot(db_session, season_id, _user_raw_picks_json())

    current_rules_config = db_session.execute(
        select(RulesConfig).where(RulesConfig.season_id == season_id, RulesConfig.is_current.is_(True))
    ).scalar_one()
    current_rules_config.rules_status = RulesStatus.BLOCKED
    db_session.commit()

    before = len(db_session.execute(select(H2HSimulationRun.id)).scalars().all())

    job = _run_h2h_simulation(db_session, snapshot.id, rival_profile.id)

    assert job.status == JobStatus.FAILED
    assert job.error_code == "RULES_BLOCKED"
    after = len(db_session.execute(select(H2HSimulationRun.id)).scalars().all())
    assert after == before


def test_h2h_simulation_missing_snapshot_id(db_session, patched_fpl_client_optimization_with_active_gameweek):
    _seed_players_with_active_gameweek(db_session)

    job = execute_job(
        db_session,
        job_type=JobType.H2H_SIMULATION,
        parameters={"rival_profile_id": str(uuid.uuid4())},
        trigger_type=TriggerType.LOCAL,
    )

    assert job.status == JobStatus.FAILED
    assert job.error_code == "MISSING_MANAGER_SQUAD_SNAPSHOT_ID"


def test_h2h_simulation_snapshot_not_found(
    db_session, patched_fpl_client_optimization_with_active_gameweek, patched_fpl_client_league
):
    season_id = _seed_players_with_active_gameweek(db_session)
    _run_league_sync(db_session)
    rival_profile = _rival_profile(db_session, season_id, LEAGUE_RIVAL_B_FPL_MANAGER_ID)

    job = _run_h2h_simulation(db_session, uuid.uuid4(), rival_profile.id)

    assert job.status == JobStatus.FAILED
    assert job.error_code == "SNAPSHOT_NOT_FOUND"


def test_h2h_simulation_rival_profile_not_found(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    season_id = _seed_players_with_active_gameweek(db_session)
    snapshot = _create_manual_user_snapshot(db_session, season_id, _user_raw_picks_json())

    job = _run_h2h_simulation(db_session, snapshot.id, uuid.uuid4())

    assert job.status == JobStatus.FAILED
    assert job.error_code == "RIVAL_PROFILE_NOT_FOUND"


def test_h2h_simulation_snapshot_missing_picks_data(
    db_session, patched_fpl_client_optimization_with_active_gameweek, patched_fpl_client_league
):
    season_id = _seed_players_with_active_gameweek(db_session)
    _run_league_sync(db_session)
    rival_profile = _rival_profile(db_session, season_id, LEAGUE_RIVAL_B_FPL_MANAGER_ID)
    # No raw_picks_json at all -- a genuinely manual squad with no picks recorded.
    snapshot = _create_manual_user_snapshot(db_session, season_id, raw_picks_json=None)

    job = _run_h2h_simulation(db_session, snapshot.id, rival_profile.id)

    assert job.status == JobStatus.FAILED
    assert job.error_code == "SNAPSHOT_MISSING_PICKS_DATA"


def test_h2h_simulation_rival_squad_unavailable_creates_no_row(
    db_session, patched_fpl_client_optimization_with_active_gameweek, patched_fpl_client_league
):
    """Rival C's picks are unavailable (404) in this fixture -- no last-known snapshot exists at all."""
    season_id = _seed_players_with_active_gameweek(db_session)
    _run_league_sync(db_session)
    rival_profile_c = _rival_profile(db_session, season_id, LEAGUE_RIVAL_C_FPL_MANAGER_ID)
    assert rival_profile_c.last_known_snapshot_id is None
    snapshot = _create_manual_user_snapshot(db_session, season_id, _user_raw_picks_json())

    before = len(db_session.execute(select(H2HSimulationRun.id)).scalars().all())

    job = _run_h2h_simulation(db_session, snapshot.id, rival_profile_c.id)

    assert job.status == JobStatus.FAILED
    assert job.error_code == "RIVAL_SQUAD_UNAVAILABLE"
    after = len(db_session.execute(select(H2HSimulationRun.id)).scalars().all())
    assert after == before
