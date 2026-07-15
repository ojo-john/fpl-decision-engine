"""Integration tests for the CHIP_SIMULATION job against a real database.

Reuses ``patched_fpl_client_optimization_with_active_gameweek`` (the
~60-player optimization pool, one unfinished gameweek -- see conftest.py) and
the same hand-picked 15-element legal squad (2 GKP/5 DEF/5 MID/3 FWD, 11
starters + 4 bench) ``tests/integration/test_chip_scenario.py`` already uses
for its own Wildcard/Free Hit MIP-rebuild tests, built into a real
``raw_picks_json`` (needed here too, since every one of the 4 chip evaluators
needs a resolved starting XI/bench/captain -- see
``app.jobs.handlers.chip_simulation``'s own docstring).

The fixture's only unfinished gameweek is the season's LAST event (38, per
the real committed ``bootstrap_static_sample.json`` chip windows: window 1
covers events 1-19, window 2 covers 20-38) -- so ``target_fpl_event_id``
always resolves to 38, always inside each chip's window 2. This keeps every
happy-path evaluation cheap (exactly one candidate gameweek per chip: the
one and only unfinished gameweek) while still exercising a real, live
``AVAILABLE`` window end to end.
"""

import uuid

from sqlalchemy import select

from app.jobs.service import execute_job
from packages.database.models.chip_optimization_run import ChipOptimizationRun
from packages.database.models.chip_recommendation import ChipRecommendation
from packages.database.models.chip_window_candidate import ChipWindowCandidate
from packages.database.models.ingestion_run import IngestionRun
from packages.database.models.manager_squad_snapshot import ManagerSquadSnapshot
from packages.database.models.player import Player
from packages.database.models.season import Season
from packages.domain.chips import ChipOptimizationRunStatus, ChipType, ChipVerdict
from packages.domain.jobs import JobStatus, JobType, TriggerType
from packages.domain.transfers import ManagerSquadSnapshotSource

# Same squad shape as test_chip_scenario.py: GKP 30001/30002, DEF 30010/30011/30019/30014/30016,
# MID 30028/30029/30030/30031/30032, FWD 30048/30049/30050.
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
_FREE_TRANSFERS_AVAILABLE = 1

_ALL_CHIP_TYPE_VALUES = frozenset(ChipType)


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


def _create_manual_snapshot(
    db_session,
    season_id,
    raw_picks_json: dict | None,
    chips_used_json: list[dict] | None = None,
    active_chip: str | None = None,
) -> ManagerSquadSnapshot:
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
        active_chip=active_chip,
        chips_used_json=chips_used_json,
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


def _run_chip_simulation(db_session, snapshot_id, **extra_parameters):
    parameters = {"manager_squad_snapshot_id": str(snapshot_id), **extra_parameters}
    return execute_job(
        db_session,
        job_type=JobType.CHIP_SIMULATION,
        parameters=parameters,
        requested_by="integration-test",
        trigger_type=TriggerType.LOCAL,
    )


def _recommendations_by_chip_type(db_session, run_id) -> dict[ChipType, ChipRecommendation]:
    rows = (
        db_session.execute(
            select(ChipRecommendation).where(ChipRecommendation.chip_optimization_run_id == run_id)
        )
        .scalars()
        .all()
    )
    return {row.chip_type: row for row in rows}


def _candidates_for_chip_type(db_session, run_id, chip_type) -> list[ChipWindowCandidate]:
    return list(
        db_session.execute(
            select(ChipWindowCandidate).where(
                ChipWindowCandidate.chip_optimization_run_id == run_id,
                ChipWindowCandidate.chip_type == chip_type,
            )
        )
        .scalars()
        .all()
    )


def test_chip_simulation_happy_path_all_four_chips_get_exactly_one_recommendation(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    season_id = _seed_players_with_active_gameweek(db_session)
    snapshot = _create_manual_snapshot(db_session, season_id, _raw_picks_json())

    job = _run_chip_simulation(db_session, snapshot.id)

    assert job.status == JobStatus.SUCCEEDED
    run_id = uuid.UUID(job.output_json["chip_optimization_run_id"])
    run_row = db_session.get(ChipOptimizationRun, run_id)
    assert run_row is not None
    assert run_row.run_status == ChipOptimizationRunStatus.SUCCEEDED
    assert run_row.manager_squad_snapshot_id == snapshot.id
    assert run_row.target_fpl_event_id == 38  # the fixture's only unfinished gameweek
    assert run_row.completed_at is not None

    recommendations = _recommendations_by_chip_type(db_session, run_id)
    assert set(recommendations.keys()) == _ALL_CHIP_TYPE_VALUES

    # Every chip's current window (20-38, per the real committed chip fixture data) is genuinely
    # open and unused -- so every chip gets a real evaluation, backed by exactly one candidate
    # (the single unfinished gameweek), never ALREADY_USED/UNAVAILABLE/EXPIRED_UNUSED.
    for chip_type, recommendation in recommendations.items():
        assert recommendation.verdict in (ChipVerdict.USE_NOW, ChipVerdict.HOLD)
        assert recommendation.rationale
        candidates = _candidates_for_chip_type(db_session, run_id, chip_type)
        assert len(candidates) == 1
        assert candidates[0].candidate_fpl_event_id == 38
        assert candidates[0].window_number == 2
        assert candidates[0].rank_within_chip_type == 1
        if recommendation.verdict == ChipVerdict.USE_NOW:
            assert recommendation.recommended_candidate_id == candidates[0].id
        else:
            # HOLD with only one (non-positive-gain) candidate never names a recommended candidate.
            assert recommendation.recommended_candidate_id is None


def test_chip_simulation_chip_already_used_gets_already_used_verdict_and_no_candidates(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    season_id = _seed_players_with_active_gameweek(db_session)
    # Wildcard already played at GW25, squarely inside window 2 (20-38).
    chips_used_json = [{"name": "wildcard", "event": 25}]
    snapshot = _create_manual_snapshot(
        db_session, season_id, _raw_picks_json(), chips_used_json=chips_used_json
    )

    job = _run_chip_simulation(db_session, snapshot.id)

    assert job.status == JobStatus.SUCCEEDED
    run_id = uuid.UUID(job.output_json["chip_optimization_run_id"])
    recommendations = _recommendations_by_chip_type(db_session, run_id)
    assert set(recommendations.keys()) == _ALL_CHIP_TYPE_VALUES

    wildcard_recommendation = recommendations[ChipType.WILDCARD]
    assert wildcard_recommendation.verdict == ChipVerdict.ALREADY_USED
    assert wildcard_recommendation.recommended_candidate_id is None
    assert _candidates_for_chip_type(db_session, run_id, ChipType.WILDCARD) == []

    # Every OTHER chip is unaffected and still gets a real evaluation.
    for chip_type in _ALL_CHIP_TYPE_VALUES - {ChipType.WILDCARD}:
        assert recommendations[chip_type].verdict in (ChipVerdict.USE_NOW, ChipVerdict.HOLD)
        assert len(_candidates_for_chip_type(db_session, run_id, chip_type)) == 1


def test_chip_simulation_active_chip_this_gameweek_gets_unavailable_verdict(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    season_id = _seed_players_with_active_gameweek(db_session)
    snapshot = _create_manual_snapshot(
        db_session, season_id, _raw_picks_json(), active_chip=ChipType.BBOOST.value
    )

    job = _run_chip_simulation(db_session, snapshot.id)

    assert job.status == JobStatus.SUCCEEDED
    run_id = uuid.UUID(job.output_json["chip_optimization_run_id"])
    recommendations = _recommendations_by_chip_type(db_session, run_id)

    assert recommendations[ChipType.BBOOST].verdict == ChipVerdict.UNAVAILABLE
    assert recommendations[ChipType.BBOOST].recommended_candidate_id is None
    assert _candidates_for_chip_type(db_session, run_id, ChipType.BBOOST) == []


def test_chip_simulation_respects_an_explicit_chip_types_subset(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    season_id = _seed_players_with_active_gameweek(db_session)
    snapshot = _create_manual_snapshot(db_session, season_id, _raw_picks_json())

    job = _run_chip_simulation(
        db_session, snapshot.id, chip_types=[ChipType.BBOOST.value, ChipType.TRIPLE_CAPTAIN.value]
    )

    assert job.status == JobStatus.SUCCEEDED
    run_id = uuid.UUID(job.output_json["chip_optimization_run_id"])
    recommendations = _recommendations_by_chip_type(db_session, run_id)
    assert set(recommendations.keys()) == {ChipType.BBOOST, ChipType.TRIPLE_CAPTAIN}


def test_chip_simulation_invalid_chip_type_rejected_creates_no_row(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    season_id = _seed_players_with_active_gameweek(db_session)
    snapshot = _create_manual_snapshot(db_session, season_id, _raw_picks_json())

    before = len(db_session.execute(select(ChipOptimizationRun.id)).scalars().all())

    job = _run_chip_simulation(db_session, snapshot.id, chip_types=["not_a_real_chip"])

    assert job.status == JobStatus.FAILED
    assert job.error_code == "INVALID_CHIP_TYPE"
    after = len(db_session.execute(select(ChipOptimizationRun.id)).scalars().all())
    assert after == before


def test_chip_simulation_off_season_refused_creates_no_row(db_session, patched_fpl_client_optimization):
    """Every gameweek finished (the plain, non-active-gameweek fixture) -- a hard refusal, no row,
    mirroring TRANSFER_OPTIMIZATION's own off-season handling (see module docstring for why chip
    timing is inherently forward-looking, unlike H2H_SIMULATION's softer fallback).
    """
    ingestion_job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert ingestion_job.status == JobStatus.SUCCEEDED
    season_id = db_session.execute(
        select(Season.id).where(Season.label == ingestion_job.output_json["season_label"])
    ).scalar_one()
    snapshot = _create_manual_snapshot(db_session, season_id, _raw_picks_json())

    before = len(db_session.execute(select(ChipOptimizationRun.id)).scalars().all())

    job = _run_chip_simulation(db_session, snapshot.id)

    assert job.status == JobStatus.FAILED
    assert job.error_code == "NO_ACTIVE_GAMEWEEK"
    after = len(db_session.execute(select(ChipOptimizationRun.id)).scalars().all())
    assert after == before


def test_chip_simulation_refused_when_optimization_not_permitted_creates_no_row(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    season_id = _seed_players_with_active_gameweek(db_session)
    snapshot = _create_manual_snapshot(db_session, season_id, _raw_picks_json())

    ingestion_run = (
        db_session.execute(select(IngestionRun).order_by(IngestionRun.completed_at.desc())).scalars().first()
    )
    ingestion_run.optimization_permitted = False
    db_session.commit()

    before = len(db_session.execute(select(ChipOptimizationRun.id)).scalars().all())

    job = _run_chip_simulation(db_session, snapshot.id)

    assert job.status == JobStatus.FAILED
    assert job.error_code == "OPTIMIZATION_NOT_PERMITTED"
    after = len(db_session.execute(select(ChipOptimizationRun.id)).scalars().all())
    assert after == before


def test_chip_simulation_missing_snapshot_id(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    _seed_players_with_active_gameweek(db_session)

    job = execute_job(
        db_session, job_type=JobType.CHIP_SIMULATION, parameters={}, trigger_type=TriggerType.LOCAL
    )

    assert job.status == JobStatus.FAILED
    assert job.error_code == "MISSING_MANAGER_SQUAD_SNAPSHOT_ID"


def test_chip_simulation_snapshot_not_found(db_session, patched_fpl_client_optimization_with_active_gameweek):
    _seed_players_with_active_gameweek(db_session)

    job = _run_chip_simulation(db_session, uuid.uuid4())

    assert job.status == JobStatus.FAILED
    assert job.error_code == "SNAPSHOT_NOT_FOUND"


def test_chip_simulation_snapshot_missing_picks_data(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    season_id = _seed_players_with_active_gameweek(db_session)
    snapshot = _create_manual_snapshot(db_session, season_id, raw_picks_json=None)

    job = _run_chip_simulation(db_session, snapshot.id)

    assert job.status == JobStatus.FAILED
    assert job.error_code == "SNAPSHOT_MISSING_PICKS_DATA"
