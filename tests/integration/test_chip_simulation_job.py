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

import random
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, update

import app.jobs.handlers.chip_simulation as chip_simulation_module
from app.chips.scenario import RecommendationResult
from app.jobs.service import execute_job
from packages.database.models.chip_optimization_run import ChipOptimizationRun
from packages.database.models.chip_recommendation import ChipRecommendation
from packages.database.models.chip_window_candidate import ChipWindowCandidate
from packages.database.models.gameweek import Gameweek
from packages.database.models.h2h_match import H2HMatch
from packages.database.models.ingestion_run import IngestionRun
from packages.database.models.league import League
from packages.database.models.manager_squad_snapshot import ManagerSquadSnapshot
from packages.database.models.player import Player
from packages.database.models.rival_gameweek_snapshot import RivalGameweekSnapshot
from packages.database.models.rival_profile import RivalProfile
from packages.database.models.season import Season
from packages.domain.chips import ChipOptimizationRunStatus, ChipType, ChipVerdict
from packages.domain.h2h import LeagueType, PicksAvailability, RivalStateConfidence
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
    fpl_manager_id: int | None = None,
    source: ManagerSquadSnapshotSource = ManagerSquadSnapshotSource.MANUAL_ENTRY,
) -> ManagerSquadSnapshot:
    """A hand-built squad snapshot. ``fpl_manager_id``/``source`` default to the plain
    ``MANUAL_ENTRY``/``fpl_manager_id=None`` shape every other test in this module uses -- passing a
    real ``fpl_manager_id`` (with ``source=MANAGER_SYNC``) is what Milestone 6 Layer 4's
    league-context refinement tests need to exercise the "which real manager is this" resolution
    (see ``app.jobs.handlers.chip_simulation``'s own docstring).
    """
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
        source=source,
        fpl_manager_id=fpl_manager_id,
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
        # Milestone 6 Layer 4: this snapshot is MANUAL_ENTRY (fpl_manager_id=None) -- no real
        # manager identity to resolve a real opponent from, so both new columns stay NULL.
        assert candidates[0].h2h_win_probability_with_chip is None
        assert candidates[0].h2h_win_probability_without_chip is None
        if recommendation.verdict == ChipVerdict.USE_NOW:
            assert recommendation.recommended_candidate_id == candidates[0].id
        else:
            # HOLD with only one (non-positive-gain) candidate never names a recommended candidate.
            assert recommendation.recommended_candidate_id is None


def _make_chip_evaluation_context(season_id, bench: list[dict], player_by_element: dict[int, Player]):
    """A minimal, valid ``_ChipEvaluationContext`` for testing ``_downgrade_bench_boost_for_
    unreliable_bench`` directly -- only ``season_id``/``bench``/``player_by_element`` are actually
    read by that function; every other field is a trivial, unused placeholder.
    """
    return chip_simulation_module._ChipEvaluationContext(
        season_id=season_id,
        starters=[],
        bench=bench,
        player_by_element=player_by_element,
        positions_by_id={},
        residuals=None,
        champion=None,
        constraints=None,
        captain_multiplier=2,
        current_squad_starting_player_ids=[],
        current_squad_captain_player_id=uuid.uuid4(),
        bank=0,
        selling_prices={},
        rng=random.Random(0),
        fpl_manager_id=None,
        now=datetime.now(UTC),
    )


def test_bench_boost_downgrades_to_hold_when_a_bench_player_has_zero_minutes(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    """Real gap this closes (found live, 2026-09-17): Bench Boost's own gain estimate already
    correctly reflects a genuine zero-minute player's near-zero individual projection, but a
    positive average across the whole bench doesn't separately surface that one or more of those
    4 players has never actually been selected to play this season -- a real reliability risk a
    manager would want called out, not silently averaged away.

    Calls ``_downgrade_bench_boost_for_unreliable_bench`` directly (not through the full job) with
    a hand-built, unambiguously-positive ``USE_NOW`` recommendation -- this module's own real
    Monte Carlo output for its fixture's specific last-gameweek fixture-count data is not a stable
    thing to assert a specific sign against (see ``test_chip_simulation_happy_path_...``'s own
    deliberately either/or verdict assertion), so this isolates exactly the real decision boundary
    under test instead of depending on that incidental variance.
    """
    season_id = _seed_players_with_active_gameweek(db_session)
    zero_minute_bench_player = db_session.execute(
        select(Player).where(Player.season_id == season_id, Player.fpl_element_id == _BENCH_ELEMENT_IDS[0])
    ).scalar_one()
    zero_minute_bench_player.minutes = 0
    db_session.commit()
    bench_picks = [{"element": eid} for eid in _BENCH_ELEMENT_IDS]
    player_by_element = {
        eid: db_session.execute(
            select(Player).where(Player.season_id == season_id, Player.fpl_element_id == eid)
        ).scalar_one()
        for eid in _BENCH_ELEMENT_IDS
    }
    ctx = _make_chip_evaluation_context(season_id, bench_picks, player_by_element)
    use_now = RecommendationResult(
        verdict=ChipVerdict.USE_NOW,
        recommended_fpl_event_id=38,
        recommended_gain_mean=9.0,
        rationale="Use now.",
    )

    result = chip_simulation_module._downgrade_bench_boost_for_unreliable_bench(
        db_session, ChipType.BBOOST, ctx, use_now
    )

    assert result.verdict == ChipVerdict.HOLD
    assert result.recommended_fpl_event_id is None
    assert result.recommended_gain_mean is None
    assert zero_minute_bench_player.web_name in result.rationale
    assert "0 minutes" in result.rationale


def test_bench_boost_leaves_use_now_unchanged_when_every_bench_player_has_played(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    season_id = _seed_players_with_active_gameweek(db_session)
    bench_picks = [{"element": eid} for eid in _BENCH_ELEMENT_IDS]
    player_by_element = {
        eid: db_session.execute(
            select(Player).where(Player.season_id == season_id, Player.fpl_element_id == eid)
        ).scalar_one()
        for eid in _BENCH_ELEMENT_IDS
    }
    ctx = _make_chip_evaluation_context(season_id, bench_picks, player_by_element)
    use_now = RecommendationResult(
        verdict=ChipVerdict.USE_NOW,
        recommended_fpl_event_id=38,
        recommended_gain_mean=9.0,
        rationale="Use now.",
    )

    result = chip_simulation_module._downgrade_bench_boost_for_unreliable_bench(
        db_session, ChipType.BBOOST, ctx, use_now
    )

    assert result == use_now


def test_bench_boost_zero_minutes_check_only_applies_to_bboost_not_other_chip_types(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    season_id = _seed_players_with_active_gameweek(db_session)
    zero_minute_bench_player = db_session.execute(
        select(Player).where(Player.season_id == season_id, Player.fpl_element_id == _BENCH_ELEMENT_IDS[0])
    ).scalar_one()
    zero_minute_bench_player.minutes = 0
    db_session.commit()
    bench_picks = [{"element": eid} for eid in _BENCH_ELEMENT_IDS]
    player_by_element = {
        eid: db_session.execute(
            select(Player).where(Player.season_id == season_id, Player.fpl_element_id == eid)
        ).scalar_one()
        for eid in _BENCH_ELEMENT_IDS
    }
    ctx = _make_chip_evaluation_context(season_id, bench_picks, player_by_element)
    use_now = RecommendationResult(
        verdict=ChipVerdict.USE_NOW,
        recommended_fpl_event_id=38,
        recommended_gain_mean=9.0,
        rationale="Use now.",
    )

    result = chip_simulation_module._downgrade_bench_boost_for_unreliable_bench(
        db_session, ChipType.TRIPLE_CAPTAIN, ctx, use_now
    )

    assert result == use_now


def test_bench_boost_zero_minutes_check_only_applies_to_use_now_not_hold(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    season_id = _seed_players_with_active_gameweek(db_session)
    zero_minute_bench_player = db_session.execute(
        select(Player).where(Player.season_id == season_id, Player.fpl_element_id == _BENCH_ELEMENT_IDS[0])
    ).scalar_one()
    zero_minute_bench_player.minutes = 0
    db_session.commit()
    bench_picks = [{"element": eid} for eid in _BENCH_ELEMENT_IDS]
    player_by_element = {
        eid: db_session.execute(
            select(Player).where(Player.season_id == season_id, Player.fpl_element_id == eid)
        ).scalar_one()
        for eid in _BENCH_ELEMENT_IDS
    }
    ctx = _make_chip_evaluation_context(season_id, bench_picks, player_by_element)
    hold = RecommendationResult(
        verdict=ChipVerdict.HOLD,
        recommended_fpl_event_id=45,
        recommended_gain_mean=12.0,
        rationale="Hold for GW45.",
    )

    result = chip_simulation_module._downgrade_bench_boost_for_unreliable_bench(
        db_session, ChipType.BBOOST, ctx, hold
    )

    assert result == hold


def test_bench_boost_zero_minutes_check_skipped_before_season_has_started(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    """The exact case the fix above must never break: pre-season, every real player genuinely has
    0 minutes -- the downgrade would otherwise fire on every single Bench Boost evaluation for a
    reason that isn't actually informative yet."""
    season_id = _seed_players_with_active_gameweek(db_session)
    db_session.execute(update(Gameweek).where(Gameweek.season_id == season_id).values(finished=False))
    zero_minute_bench_player = db_session.execute(
        select(Player).where(Player.season_id == season_id, Player.fpl_element_id == _BENCH_ELEMENT_IDS[0])
    ).scalar_one()
    zero_minute_bench_player.minutes = 0
    db_session.commit()
    bench_picks = [{"element": eid} for eid in _BENCH_ELEMENT_IDS]
    player_by_element = {
        eid: db_session.execute(
            select(Player).where(Player.season_id == season_id, Player.fpl_element_id == eid)
        ).scalar_one()
        for eid in _BENCH_ELEMENT_IDS
    }
    ctx = _make_chip_evaluation_context(season_id, bench_picks, player_by_element)
    use_now = RecommendationResult(
        verdict=ChipVerdict.USE_NOW,
        recommended_fpl_event_id=1,
        recommended_gain_mean=9.0,
        rationale="Use now.",
    )

    result = chip_simulation_module._downgrade_bench_boost_for_unreliable_bench(
        db_session, ChipType.BBOOST, ctx, use_now
    )

    assert result == use_now


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


def test_chip_simulation_snapshot_missing_picks_data_falls_back_to_solved_lineup(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    """A snapshot with no raw_picks_json (every MANUAL_ENTRY squad -- see that model's own
    docstring) can't have its starting XI read verbatim, but CHIP_SIMULATION must not just refuse
    outright: _derive_starting_and_bench_from_solver solves one instead, from the same feasible
    15-player squad _raw_picks_json() would have described by hand."""
    season_id = _seed_players_with_active_gameweek(db_session)
    snapshot = _create_manual_snapshot(db_session, season_id, raw_picks_json=None)

    job = _run_chip_simulation(db_session, snapshot.id)

    assert job.status == JobStatus.SUCCEEDED
    run_id = uuid.UUID(job.output_json["chip_optimization_run_id"])
    run_row = db_session.get(ChipOptimizationRun, run_id)
    assert run_row is not None
    assert run_row.run_status == ChipOptimizationRunStatus.SUCCEEDED

    recommendations = _recommendations_by_chip_type(db_session, run_id)
    assert set(recommendations.keys()) == _ALL_CHIP_TYPE_VALUES
    for recommendation in recommendations.values():
        assert recommendation.verdict in (ChipVerdict.USE_NOW, ChipVerdict.HOLD)
        assert recommendation.rationale


def test_chip_simulation_snapshot_with_unsolvable_squad_still_fails_cleanly(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    """The solver fallback degrades to the same clean failure as before when there's genuinely
    nothing to solve -- an empty squad can never produce a feasible starting XI."""
    season_id = _seed_players_with_active_gameweek(db_session)
    snapshot = _create_manual_snapshot(db_session, season_id, raw_picks_json=None)
    snapshot.squad_player_ids = []
    db_session.commit()

    job = _run_chip_simulation(db_session, snapshot.id)

    assert job.status == JobStatus.FAILED
    assert job.error_code == "CHIP_SIMULATION_FAILED"


# --- Milestone 6 Layer 4: league-context refinement ---------------------------------------------
#
# A real opponent needs a real, resolvable fpl_manager_id on the user's OWN snapshot (see
# _create_manual_snapshot's fpl_manager_id/source overrides) plus a real, scheduled h2h_matches row
# + a synced rival_profiles/rival_gameweek_snapshots pair for the opponent -- see
# app.jobs.handlers.chip_simulation's own module docstring for the full resolution chain.

# Disjoint from _SQUAD_ELEMENT_IDS (the user's own squad) so the two sides are visibly distinct
# squads, drawn from the same ~60-player synthetic pool (element ids 30000-30059).
_RIVAL_SQUAD_ELEMENT_IDS = [
    30003,
    30004,
    30012,
    30015,
    30018,
    30020,
    30021,
    30033,
    30035,
    30036,
    30037,
    30038,
    30053,
    30054,
    30055,
]
_RIVAL_STARTING_ELEMENT_IDS = [30003, 30012, 30015, 30018, 30020, 30033, 30035, 30036, 30037, 30053, 30054]
_RIVAL_BENCH_ELEMENT_IDS = [30004, 30021, 30038, 30055]
_RIVAL_CAPTAIN_ELEMENT_ID = 30053
_RIVAL_VICE_CAPTAIN_ELEMENT_ID = 30054

_USER_REAL_FPL_MANAGER_ID = 910001
_RIVAL_REAL_FPL_MANAGER_ID = 910002
# Randomized (not a fixed literal) so separate pytest invocations against the same
# non-truncated Postgres never collide on League.fpl_league_id's / H2HMatch.fpl_match_id's
# own UNIQUE constraints -- same rationale, same fix shape, as conftest.py's own
# secrets.randbelow-seeded season-year counter.
_REAL_LEAGUE_FPL_ID = 910100 + secrets.randbelow(1_000_000)
_REAL_H2H_MATCH_FPL_ID = 920001 + secrets.randbelow(1_000_000)

# > _WILDCARD_MAX_CANDIDATE_GAMEWEEKS (4) would exercise the cap; deliberately small here instead
# (well within the cap) so every one of the 4 chip types genuinely evaluates the SAME 3 candidate
# gameweeks, letting one shared assertion block check "immediate populated, a later one NULL"
# uniformly across all 4 -- not just the 2 exhaustively-evaluated ones.
_TRAILING_UNFINISHED_GAMEWEEKS = 3


def _rival_raw_picks_json() -> dict:
    picks = []
    for position, element in enumerate(_RIVAL_STARTING_ELEMENT_IDS, start=1):
        picks.append(
            {
                "element": element,
                "position": position,
                "multiplier": 2 if element == _RIVAL_CAPTAIN_ELEMENT_ID else 1,
                "is_captain": element == _RIVAL_CAPTAIN_ELEMENT_ID,
                "is_vice_captain": element == _RIVAL_VICE_CAPTAIN_ELEMENT_ID,
            }
        )
    for offset, element in enumerate(_RIVAL_BENCH_ELEMENT_IDS):
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


def _mark_trailing_gameweeks_unfinished(db_session, season_id, count: int) -> list[int]:
    """Mark the trailing ``count`` gameweeks (by ``fpl_event_id``) unfinished WITH real ascending
    future deadlines, returning their ``fpl_event_id``s ascending -- mirrors ``test_chip_scenario.py``'s
    own ``_seed_season_with_several_unfinished_gameweeks`` trick, so a chip's window genuinely
    contains several candidate gameweeks instead of the fixture's usual single trailing one (needed
    here to exercise "the immediate candidate gets populated, a LATER one stays NULL"). Real
    deadlines matter here, not just ``finished=False``: ``app.optimization.gameweek_resolution``
    resolves candidates by ``deadline_time > now``.
    """
    trailing = db_session.execute(
        select(Gameweek.id, Gameweek.fpl_event_id)
        .where(Gameweek.season_id == season_id)
        .order_by(Gameweek.fpl_event_id.desc())
        .limit(count)
    ).all()
    assert len(trailing) == count
    now = datetime.now(UTC)
    # Ascending deadlines matching ascending fpl_event_id (trailing is currently DESC-ordered).
    for offset, (gameweek_id, _fpl_event_id) in enumerate(reversed(trailing)):
        db_session.execute(
            update(Gameweek)
            .where(Gameweek.id == gameweek_id)
            .values(finished=False, deadline_time=now + timedelta(days=7 * (offset + 1)))
        )
    db_session.commit()
    return sorted(fpl_event_id for _gameweek_id, fpl_event_id in trailing)


def _seed_real_opponent(
    db_session, season_id, target_fpl_event_id: int, user_fpl_manager_id: int, rival_fpl_manager_id: int
) -> None:
    """Seed the minimal real-data shape Milestone 6 Layer 4 resolves against: a real, scheduled
    ``h2h_matches`` row naming both managers at ``target_fpl_event_id``, plus a synced
    ``rival_profiles`` row for the opponent pointing at a usable ``rival_gameweek_snapshots`` row.
    """
    now = datetime.now(UTC)
    league = League(
        fpl_league_id=_REAL_LEAGUE_FPL_ID,
        name="Test H2H League",
        league_type=LeagueType.H2H,
        scoring="h",
        admin_entry=user_fpl_manager_id,
        start_event=1,
        ko_rounds=None,
        closed=False,
        has_cup=False,
        raw_league_json=None,
        last_synced_at=now,
    )
    db_session.add(league)
    db_session.flush()

    db_session.add(
        H2HMatch(
            fpl_match_id=_REAL_H2H_MATCH_FPL_ID,
            league_id=league.id,
            event=target_fpl_event_id,
            entry_1_id=user_fpl_manager_id,
            entry_2_id=rival_fpl_manager_id,
            entry_1_points=None,
            entry_2_points=None,
            entry_1_win=None,
            entry_1_draw=None,
            entry_1_loss=None,
            entry_2_win=None,
            entry_2_draw=None,
            entry_2_loss=None,
            winner=None,
            is_knockout=False,
            knockout_name=None,
            is_bye=False,
            seed_value=None,
            raw_match_json=None,
            last_synced_at=now,
        )
    )

    rival_snapshot = RivalGameweekSnapshot(
        fpl_manager_id=rival_fpl_manager_id,
        fpl_event_id=target_fpl_event_id,
        as_of=now,
        picks_availability=PicksAvailability.AVAILABLE,
        http_status=200,
        active_chip=None,
        captain_element_id=_RIVAL_CAPTAIN_ELEMENT_ID,
        vice_captain_element_id=_RIVAL_VICE_CAPTAIN_ELEMENT_ID,
        squad_player_ids=None,
        unresolved_fpl_element_ids_json=None,
        event_points=None,
        event_transfers=None,
        event_transfers_cost=None,
        overall_rank=None,
        raw_picks_json=_rival_raw_picks_json(),
    )
    db_session.add(rival_snapshot)
    db_session.flush()

    db_session.add(
        RivalProfile(
            season_id=season_id,
            fpl_manager_id=rival_fpl_manager_id,
            league_id=league.id,
            last_known_snapshot_id=rival_snapshot.id,
            derived_free_transfers_min=None,
            derived_free_transfers_max=None,
            derived_free_transfers_confidence=None,
            derived_likely_remaining_chips_json=None,
            predicted_no_transfer_probability=Decimal("1.0000"),
            predicted_one_transfer_probability=Decimal("0.0000"),
            predicted_multi_transfer_probability=Decimal("0.0000"),
            predicted_hit_probability=Decimal("0.0000"),
            predicted_captain_probabilities_json={str(_RIVAL_CAPTAIN_ELEMENT_ID): 1.0},
            predicted_likely_transfer_targets_json=[],
            tendency_features_json=None,
            unknowns_json=None,
            evidence_json=None,
            overall_confidence=RivalStateConfidence.LOW,
            computed_at=now,
        )
    )
    db_session.commit()


def test_chip_simulation_immediate_candidate_gets_h2h_win_probabilities_when_real_opponent_resolves(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    season_id = _seed_players_with_active_gameweek(db_session)
    trailing_events = _mark_trailing_gameweeks_unfinished(
        db_session, season_id, _TRAILING_UNFINISHED_GAMEWEEKS
    )
    immediate_event = trailing_events[0]
    later_event = trailing_events[-1]
    assert later_event != immediate_event

    snapshot = _create_manual_snapshot(
        db_session,
        season_id,
        _raw_picks_json(),
        fpl_manager_id=_USER_REAL_FPL_MANAGER_ID,
        source=ManagerSquadSnapshotSource.MANAGER_SYNC,
    )
    _seed_real_opponent(
        db_session, season_id, immediate_event, _USER_REAL_FPL_MANAGER_ID, _RIVAL_REAL_FPL_MANAGER_ID
    )

    job = _run_chip_simulation(db_session, snapshot.id)

    assert job.status == JobStatus.SUCCEEDED
    run_id = uuid.UUID(job.output_json["chip_optimization_run_id"])
    assert job.output_json["target_fpl_event_id"] == immediate_event

    for chip_type in _ALL_CHIP_TYPE_VALUES:
        candidates_by_event = {
            c.candidate_fpl_event_id: c for c in _candidates_for_chip_type(db_session, run_id, chip_type)
        }
        assert immediate_event in candidates_by_event, chip_type

        immediate_candidate = candidates_by_event[immediate_event]
        assert immediate_candidate.h2h_win_probability_with_chip is not None, chip_type
        assert immediate_candidate.h2h_win_probability_without_chip is not None, chip_type
        assert Decimal("0") <= immediate_candidate.h2h_win_probability_with_chip <= Decimal("1")
        assert Decimal("0") <= immediate_candidate.h2h_win_probability_without_chip <= Decimal("1")

        # A LATER candidate (a real opponent for a future gameweek is never knowable in advance)
        # always stays NULL -- only ever populated for the immediate candidate.
        if later_event in candidates_by_event:
            later_candidate = candidates_by_event[later_event]
            assert later_candidate.h2h_win_probability_with_chip is None, chip_type
            assert later_candidate.h2h_win_probability_without_chip is None, chip_type


def test_chip_simulation_h2h_win_probabilities_stay_null_when_no_real_opponent_resolves(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    """A real, resolvable ``fpl_manager_id`` alone is not enough -- with no scheduled ``h2h_matches``
    row for this gameweek at all (off-season/bye-week/never-synced-league), the refinement is
    skipped entirely and the run still succeeds normally, unaffected.
    """
    season_id = _seed_players_with_active_gameweek(db_session)
    snapshot = _create_manual_snapshot(
        db_session,
        season_id,
        _raw_picks_json(),
        fpl_manager_id=_USER_REAL_FPL_MANAGER_ID,
        source=ManagerSquadSnapshotSource.MANAGER_SYNC,
    )

    job = _run_chip_simulation(db_session, snapshot.id)

    assert job.status == JobStatus.SUCCEEDED
    run_id = uuid.UUID(job.output_json["chip_optimization_run_id"])
    recommendations = _recommendations_by_chip_type(db_session, run_id)
    assert set(recommendations.keys()) == _ALL_CHIP_TYPE_VALUES

    for chip_type, recommendation in recommendations.items():
        assert recommendation.verdict in (ChipVerdict.USE_NOW, ChipVerdict.HOLD)
        candidates = _candidates_for_chip_type(db_session, run_id, chip_type)
        assert len(candidates) == 1
        assert candidates[0].candidate_fpl_event_id == 38
        assert candidates[0].h2h_win_probability_with_chip is None
        assert candidates[0].h2h_win_probability_without_chip is None
