"""Integration tests for the RECOMMENDATION_GENERATION job against a real database.

Reuses ``patched_fpl_client_optimization_with_active_gameweek`` (the
~60-player optimization pool, one unfinished gameweek -- event 38, see
conftest.py) for the underlying SQUAD_OPTIMIZATION/TRANSFER_OPTIMIZATION/
CHIP_SIMULATION component runs, and ``patched_fpl_client_league`` (the
synthetic 4-entry H2H league) via a real LEAGUE_SYNC run for the
H2H_SIMULATION component -- exactly the same fixture combination
``test_h2h_simulation_job.py`` already uses. A champion ``ModelVersion`` row
is inserted directly (``is_champion=True``) rather than run through a real
MODEL_TRAINING pass -- RECOMMENDATION_GENERATION only ever does a direct
``is_champion IS TRUE`` read (see that handler's own docstring), so there's
nothing champion-loader-specific worth exercising here.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update

from app.jobs.service import execute_job
from packages.database.models.chip_optimization_run import ChipOptimizationRun
from packages.database.models.h2h_simulation_run import H2HSimulationRun
from packages.database.models.manager_squad_snapshot import ManagerSquadSnapshot
from packages.database.models.model_version import ModelVersion
from packages.database.models.optimization_run import OptimizationRun
from packages.database.models.player import Player
from packages.database.models.recommendation_audit import RecommendationAudit
from packages.database.models.rival_profile import RivalProfile
from packages.database.models.season import Season
from packages.database.models.transfer_optimization_run import TransferOptimizationRun
from packages.domain.chips import ChipOptimizationRunStatus
from packages.domain.h2h import H2HSimulationRunStatus
from packages.domain.jobs import JobStatus, JobType, TriggerType
from packages.domain.learning import RecommendationConfidence
from packages.domain.modeling import ModelFamily
from packages.domain.optimization import OptimizationRunStatus
from packages.domain.transfers import ManagerSquadSnapshotSource, TransferOptimizationRunStatus
from tests.integration.conftest import LEAGUE_RIVAL_B_FPL_MANAGER_ID, LEAGUE_SELF_FPL_MANAGER_ID

FPL_LEAGUE_ID = 88893  # distinct from every other test module's own league id

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
_VICE_CAPTAIN_ELEMENT_ID = 30049
_BANK = 20
_FREE_TRANSFERS_AVAILABLE = 2


def _raw_picks_json() -> dict:
    picks = []
    for position, element in enumerate(_STARTING_ELEMENT_IDS, start=1):
        picks.append(
            {
                "element": element,
                "position": position,
                "multiplier": 2 if element == _CAPTAIN_ELEMENT_ID else 1,
                "is_captain": element == _CAPTAIN_ELEMENT_ID,
                "is_vice_captain": element == _VICE_CAPTAIN_ELEMENT_ID,
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


def _seed_players_with_active_gameweek(db_session):
    ingestion_job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert ingestion_job.status == JobStatus.SUCCEEDED
    season_id = db_session.execute(
        select(Season.id).where(Season.label == ingestion_job.output_json["season_label"])
    ).scalar_one()
    return season_id


def _seed_players_off_season(db_session):
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


def _seed_champion_model_version(db_session) -> ModelVersion:
    # is_champion is a single, GLOBAL flag (no season scoping, see ModelVersion's own docstring,
    # and app.optimization.champion_loader.load_current_champion's own scalar_one_or_none() query
    # that assumes at most one True row exists across the WHOLE table) -- clear any prior champion
    # first rather than assume a pristine DB, exactly like test_optimization_uses_champion.py's own
    # discipline, since this same persistent local Postgres is reused across every test/run in this
    # module (and the wider suite).
    db_session.execute(update(ModelVersion).values(is_champion=False))

    model_version = ModelVersion(
        model_name="baseline-season-average",
        model_family=ModelFamily.BASELINE_SEASON_AVERAGE,
        feature_version=1,
        target_variable="total_points_next_gw",
        training_seasons_json=["2099-00"],
        hyperparameters_json=None,
        artifact_uri=None,
        artifact_checksum_sha256=None,
        is_champion=True,
        trained_at=datetime.now(UTC),
        training_job_id=None,
    )
    db_session.add(model_version)
    db_session.commit()
    db_session.refresh(model_version)
    return model_version


def _run_squad_optimization(db_session):
    return execute_job(db_session, job_type=JobType.SQUAD_OPTIMIZATION, trigger_type=TriggerType.LOCAL)


def _run_transfer_optimization(db_session, snapshot_id: uuid.UUID):
    return execute_job(
        db_session,
        job_type=JobType.TRANSFER_OPTIMIZATION,
        parameters={"manager_squad_snapshot_id": str(snapshot_id), "horizon_gameweeks": 3},
        requested_by="integration-test",
        trigger_type=TriggerType.LOCAL,
    )


def _run_chip_simulation(db_session, snapshot_id: uuid.UUID):
    return execute_job(
        db_session,
        job_type=JobType.CHIP_SIMULATION,
        parameters={"manager_squad_snapshot_id": str(snapshot_id)},
        requested_by="integration-test",
        trigger_type=TriggerType.LOCAL,
    )


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


def _run_h2h_simulation(db_session, snapshot_id: uuid.UUID, rival_profile_id: uuid.UUID):
    return execute_job(
        db_session,
        job_type=JobType.H2H_SIMULATION,
        parameters={
            "manager_squad_snapshot_id": str(snapshot_id),
            "rival_profile_id": str(rival_profile_id),
        },
        requested_by="integration-test",
        trigger_type=TriggerType.LOCAL,
    )


def _run_recommendation_generation(db_session, snapshot_id: uuid.UUID | None = None, **extra_parameters):
    parameters: dict = dict(extra_parameters)
    if snapshot_id is not None:
        parameters["manager_squad_snapshot_id"] = str(snapshot_id)
    return execute_job(
        db_session,
        job_type=JobType.RECOMMENDATION_GENERATION,
        parameters=parameters or None,
        requested_by="integration-test",
        trigger_type=TriggerType.LOCAL,
    )


def test_recommendation_generation_happy_path_correlates_all_four_components(
    db_session, patched_fpl_client_optimization_with_active_gameweek, patched_fpl_client_league
):
    season_id = _seed_players_with_active_gameweek(db_session)
    champion = _seed_champion_model_version(db_session)

    squad_job = _run_squad_optimization(db_session)
    assert squad_job.status == JobStatus.SUCCEEDED
    squad_run_id = uuid.UUID(squad_job.output_json["optimization_run_id"])

    snapshot = _create_manual_snapshot(db_session, season_id)

    transfer_job = _run_transfer_optimization(db_session, snapshot.id)
    assert transfer_job.status == JobStatus.SUCCEEDED
    transfer_run_id = uuid.UUID(transfer_job.output_json["transfer_optimization_run_id"])

    chip_job = _run_chip_simulation(db_session, snapshot.id)
    assert chip_job.status == JobStatus.SUCCEEDED
    chip_run_id = uuid.UUID(chip_job.output_json["chip_optimization_run_id"])

    league_job = _run_league_sync(db_session)
    assert league_job.status == JobStatus.SUCCEEDED
    rival_profile = _rival_profile(db_session, season_id, LEAGUE_RIVAL_B_FPL_MANAGER_ID)
    assert rival_profile.last_known_snapshot_id is not None

    h2h_job = _run_h2h_simulation(db_session, snapshot.id, rival_profile.id)
    assert h2h_job.status == JobStatus.SUCCEEDED
    h2h_run_id = uuid.UUID(h2h_job.output_json["h2h_simulation_run_id"])

    rec_job = _run_recommendation_generation(db_session, snapshot.id)

    assert rec_job.status == JobStatus.SUCCEEDED
    output = rec_job.output_json
    audit = db_session.get(RecommendationAudit, uuid.UUID(output["recommendation_audit_id"]))
    assert audit is not None
    assert audit.season_id == season_id
    assert audit.target_fpl_event_id == 38
    assert audit.is_authoritative is True
    assert audit.optimization_run_id == squad_run_id
    assert audit.transfer_optimization_run_id == transfer_run_id
    assert audit.chip_optimization_run_id == chip_run_id
    assert audit.h2h_simulation_run_id == h2h_run_id
    assert audit.champion_model_version_id == champion.id
    assert audit.confidence == RecommendationConfidence.HIGH
    assert audit.confidence_drivers_json == [
        "resolved_components=4/4",
        "fpl_api_health=healthy",
        "data_quality_score=100.00",
    ]
    assert audit.known_risks_json == []
    assert len(audit.assumptions_json) == 2
    assert audit.user_actual_decision_json is None
    assert audit.generated_at is not None

    # Sanity-check every referenced component run really is the exact one just produced.
    assert db_session.get(OptimizationRun, squad_run_id).run_status == OptimizationRunStatus.SUCCEEDED
    assert (
        db_session.get(TransferOptimizationRun, transfer_run_id).run_status
        == TransferOptimizationRunStatus.SUCCEEDED
    )
    assert db_session.get(ChipOptimizationRun, chip_run_id).run_status == ChipOptimizationRunStatus.SUCCEEDED
    assert db_session.get(H2HSimulationRun, h2h_run_id).run_status == H2HSimulationRunStatus.SUCCEEDED


def test_recommendation_generation_partial_availability_leaves_missing_components_null(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    season_id = _seed_players_with_active_gameweek(db_session)

    # is_champion is a single, GLOBAL flag (no season scoping, see ModelVersion's own docstring)
    # -- explicitly clear it first rather than assume a pristine DB, exactly like
    # test_optimization_uses_champion.py's own "no champion" test does, since an earlier test in
    # this same module (or an earlier module in the same suite run) may have left one behind on
    # this persistent, shared local Postgres.
    db_session.execute(update(ModelVersion).values(is_champion=False))
    db_session.commit()

    squad_job = _run_squad_optimization(db_session)
    assert squad_job.status == JobStatus.SUCCEEDED
    squad_run_id = uuid.UUID(squad_job.output_json["optimization_run_id"])

    snapshot = _create_manual_snapshot(db_session, season_id)

    transfer_job = _run_transfer_optimization(db_session, snapshot.id)
    assert transfer_job.status == JobStatus.SUCCEEDED
    transfer_run_id = uuid.UUID(transfer_job.output_json["transfer_optimization_run_id"])

    # Deliberately never run CHIP_SIMULATION or H2H_SIMULATION (nor seed a champion model) --
    # a realistic partial-availability state (e.g. no H2H opponent this gameweek yet, chips not
    # evaluated yet, no model trained yet).
    rec_job = _run_recommendation_generation(db_session, snapshot.id)

    assert rec_job.status == JobStatus.SUCCEEDED
    audit = db_session.get(RecommendationAudit, uuid.UUID(rec_job.output_json["recommendation_audit_id"]))
    assert audit.optimization_run_id == squad_run_id
    assert audit.transfer_optimization_run_id == transfer_run_id
    assert audit.h2h_simulation_run_id is None
    assert audit.chip_optimization_run_id is None
    assert audit.champion_model_version_id is None
    assert audit.confidence == RecommendationConfidence.MEDIUM
    assert len(audit.known_risks_json) == 3  # H2H, chip, and no-champion risks
    assert any("H2H simulation" in risk for risk in audit.known_risks_json)
    assert any("Chip timing" in risk for risk in audit.known_risks_json)
    assert any("champion model" in risk for risk in audit.known_risks_json)


def test_recommendation_generation_off_season_refuses_with_no_row_created(
    db_session, patched_fpl_client_optimization
):
    season_id = _seed_players_off_season(db_session)

    job = _run_recommendation_generation(db_session)

    assert job.status == JobStatus.FAILED
    assert job.error_code == "NO_ACTIVE_GAMEWEEK"
    assert job.output_json is None

    # Scoped to THIS test's own fresh season (a unique synthetic label per unique_bootstrap_json,
    # see conftest.py) rather than asserting the whole table is empty -- other tests in this same
    # module/suite run against the same persistent local Postgres and legitimately leave rows
    # behind for their own, different seasons.
    rows_for_this_season = (
        db_session.execute(select(RecommendationAudit).where(RecommendationAudit.season_id == season_id))
        .scalars()
        .all()
    )
    assert rows_for_this_season == []


def test_recommendation_generation_rerun_flips_previous_authoritative_row(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    season_id = _seed_players_with_active_gameweek(db_session)
    _run_squad_optimization(db_session)
    snapshot = _create_manual_snapshot(db_session, season_id)
    _run_transfer_optimization(db_session, snapshot.id)

    first_job = _run_recommendation_generation(db_session, snapshot.id)
    assert first_job.status == JobStatus.SUCCEEDED
    first_audit_id = uuid.UUID(first_job.output_json["recommendation_audit_id"])

    second_job = _run_recommendation_generation(db_session, snapshot.id)
    assert second_job.status == JobStatus.SUCCEEDED
    second_audit_id = uuid.UUID(second_job.output_json["recommendation_audit_id"])

    assert first_audit_id != second_audit_id

    db_session.expire_all()
    first_audit = db_session.get(RecommendationAudit, first_audit_id)
    second_audit = db_session.get(RecommendationAudit, second_audit_id)
    assert first_audit.is_authoritative is False
    assert second_audit.is_authoritative is True
    assert first_audit.season_id == second_audit.season_id
    assert first_audit.target_gameweek_id == second_audit.target_gameweek_id


def test_recommendation_generation_missing_snapshot_returns_error(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    _seed_players_with_active_gameweek(db_session)

    job = _run_recommendation_generation(db_session, uuid.uuid4())

    assert job.status == JobStatus.FAILED
    assert job.error_code == "SNAPSHOT_NOT_FOUND"


def test_recommendation_generation_explicit_target_event_overrides_default_resolution(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    season_id = _seed_players_with_active_gameweek(db_session)
    _run_squad_optimization(db_session)
    snapshot = _create_manual_snapshot(db_session, season_id)

    job = _run_recommendation_generation(db_session, snapshot.id, target_fpl_event_id=38)

    assert job.status == JobStatus.SUCCEEDED
    assert job.output_json["target_fpl_event_id"] == 38
