"""Integration tests for app.learning.outcome_ingestion.ingest_outcome_for_audit against a real DB.

Component runs (``OptimizationRun``/``TransferOptimizationRun``/
``ChipOptimizationRun``/``H2HSimulationRun`` and their children) are built
directly via the ORM rather than by driving the 5 real job handlers end to
end: ``ingest_outcome_for_audit`` only ever reads already-persisted rows by
FK, so it cannot tell the difference, and hand-building them is the only
practical way to deterministically exercise every one of the 8
``CounterfactualComparisonType``s at once (e.g. forcing the recommended
transfer scenario to have actually taken a real hit, or the chip
recommendation to be ``USE_NOW`` for exactly this gameweek) without
reverse-engineering each optimizer's own solver/ranking internals. Real
``Player``/``Season``/``Gameweek``/``RulesConfig`` rows are still seeded via a
real ``DATA_INGESTION`` run (``patched_fpl_client_optimization``, the
60-player pool already used by every other optimizer integration test in
this suite) so every FK in this test is a genuine row in a genuine Postgres,
not a mock.

All hand-picked ``player_gameweek_stats.total_points`` values below are
chosen so every comparison lands on a distinct, non-tie, hand-verified delta
(ties/zero-delta edge cases are already covered by
``tests/unit/test_counterfactuals.py``) -- see the arithmetic laid out in
each seed function's own comment.
"""

import itertools
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from app.jobs.service import execute_job
from app.learning.outcome_ingestion import (
    GameweekNotFinishedError,
    RecommendationAuditNotFoundError,
    ingest_outcome_for_audit,
)
from packages.database.models.chip_optimization_run import ChipOptimizationRun
from packages.database.models.chip_recommendation import ChipRecommendation
from packages.database.models.chip_window_candidate import ChipWindowCandidate
from packages.database.models.counterfactual_result import CounterfactualResult
from packages.database.models.gameweek import Gameweek
from packages.database.models.h2h_match import H2HMatch
from packages.database.models.h2h_simulation_run import H2HSimulationRun
from packages.database.models.h2h_strategy_candidate import H2HStrategyCandidate
from packages.database.models.league import League
from packages.database.models.manager_squad_snapshot import ManagerSquadSnapshot
from packages.database.models.optimization_run import OptimizationRun
from packages.database.models.player import Player
from packages.database.models.player_gameweek_stats import PlayerGameweekStats
from packages.database.models.recommendation_audit import RecommendationAudit
from packages.database.models.recommendation_outcome import RecommendationOutcome
from packages.database.models.rival_gameweek_snapshot import RivalGameweekSnapshot
from packages.database.models.rival_profile import RivalProfile
from packages.database.models.rules_config import RulesConfig
from packages.database.models.season import Season
from packages.database.models.transfer_optimization_run import TransferOptimizationRun
from packages.database.models.transfer_optimization_run_move import TransferOptimizationRunMove
from packages.database.models.transfer_optimization_run_scenario import TransferOptimizationRunScenario
from packages.domain.chips import ChipOptimizationRunStatus, ChipType, ChipVerdict
from packages.domain.h2h import (
    H2HSimulationRunStatus,
    H2HStrategyLabel,
    LeagueType,
    PicksAvailability,
    RivalStateConfidence,
)
from packages.domain.jobs import JobStatus, JobType, TriggerType
from packages.domain.learning import CounterfactualComparisonType, RecommendationConfidence
from packages.domain.optimization import (
    OptimizationRunStatus,
    RecommendationType,
    SolverStatus,
    TargetGameweekStatus,
)
from packages.domain.rules import RulesStatus
from packages.domain.transfers import ManagerSquadSnapshotSource, TransferOptimizationRunStatus

# leagues.fpl_league_id/h2h_matches.fpl_match_id are globally unique, and _resolve_h2h_match's own
# query matches on (event, entry_1_id, entry_2_id) with no league/season scoping at all -- so a
# fixed USER/RIVAL fpl_manager_id pair would collide across the multiple _build_full_scenario calls
# in this module. Wall-clock-seeded (mirrors conftest.py's own _season_year_counter technique) since
# this persistent, shared local Postgres never truncates between either tests OR separate pytest
# process invocations -- a fixed starting constant would collide across re-runs, not just within one.
_id_seed = int(time.time()) % 1_000_000
_league_id_counter = itertools.count(600_000 + _id_seed)
_h2h_match_id_counter = itertools.count(700_000 + _id_seed)
_manager_id_counter = itertools.count(800_000 + _id_seed, 2)  # USER = n, RIVAL = n + 1, always a fresh pair

# --- player_gameweek_stats.total_points seed values, see module docstring ---------------------------
# P1..P11: the base squad every comparison below is built from (used, with different captains/subs,
# as BOTH the recommended XI and the H2H base squad).
_POINTS_BY_SLOT = {
    "P1": 10,  # recommended captain
    "P2": 8,  # actual captain
    "P3": 5,  # H2H "DIFFERENTIAL_CAPTAIN"/"HIGH_UPSIDE" alternative captain
    "P4": 5,  # H2H "HIGH_UPSIDE" alternative captain (CONSERVATIVE_VS_AGGRESSIVE)
    "P5": 5,
    "P6": 5,
    "P7": 5,
    "P8": 5,
    "P9": 5,
    "P10": 5,
    "P11": 3,  # only in the recommended XI (actual XI subs P12 in instead)
    "P12": 6,  # only in the actual XI
    "P13": 2,  # actual bench
    "P14": 1,  # actual bench
    "P15": 0,  # actual bench
    "P16": 3,  # actual bench
    "P20": 4,  # transferred out (both 1- and 2-transfer scenarios)
    "P21": 9,  # transferred in (both scenarios)
    "P22": 3,  # transferred out (2-transfer scenario only)
    "P23": 8,  # transferred in (2-transfer scenario only)
}
# Elements 30000.. are always resolvable Players in the seeded 60-player pool (see
# tests/integration/conftest.py's build_optimization_bootstrap_json).
_ELEMENT_BY_SLOT = {
    "P1": 30000,
    "P2": 30001,
    "P3": 30002,
    "P4": 30003,
    "P5": 30004,
    "P6": 30005,
    "P7": 30006,
    "P8": 30007,
    "P9": 30008,
    "P10": 30009,
    "P11": 30010,
    "P12": 30011,
    "P13": 30012,
    "P14": 30013,
    "P15": 30014,
    "P16": 30015,
    "P20": 30020,
    "P21": 30021,
    "P22": 30022,
    "P23": 30023,
}

# Hand-verified expected arithmetic (see docstring for the reasoning) -----------------------------
# RECOMMENDED_VS_ACTUAL_TEAM: baseline(actual)=72, alternative(recommended)=71, delta=-1
# RECOMMENDED_VS_ACTUAL_CAPTAIN: baseline=16, alternative=20, delta=4
# TRANSFER_VS_NO_TRANSFER: baseline(roll)=0, alternative(recommended, 2-transfer hit)=6, delta=6
# TRANSFER_VS_TOP_ALTERNATIVE: baseline(1-transfer)=5, alternative=6, delta=1
# HIT_VS_NO_HIT: baseline(1-transfer, zero hit)=5, alternative=6, delta=1
# CHIP_VS_HOLD: baseline=0, alternative(bench realized)=6, delta=6
# OVERALL_VS_H2H_ADJUSTED: baseline(EP_OPTIMAL)=71, alternative(recommended candidate)=66, delta=-5
# CONSERVATIVE_VS_AGGRESSIVE: baseline(LOW_VARIANCE)=71, alternative(HIGH_UPSIDE)=66, delta=-5


def _now():
    return datetime.now(UTC)


def _seed_players(db_session) -> uuid.UUID:
    """Seed a real 60-player pool via a real DATA_INGESTION run.

    Relies on ``patched_fpl_client_optimization`` (autouse-free, required as a fixture argument by
    every test in this module) being active so ``FPLClient`` never hits the real network -- see
    ``tests/integration/conftest.py``'s own docstring for that fixture.
    """
    ingestion_job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert ingestion_job.status == JobStatus.SUCCEEDED
    return db_session.execute(
        select(Season.id).where(Season.label == ingestion_job.output_json["season_label"])
    ).scalar_one()


def _player_id(db_session, season_id: uuid.UUID, slot: str) -> uuid.UUID:
    return db_session.execute(
        select(Player.id).where(
            Player.season_id == season_id, Player.fpl_element_id == _ELEMENT_BY_SLOT[slot]
        )
    ).scalar_one()


def _player_fpl_element_id(slot: str) -> int:
    return _ELEMENT_BY_SLOT[slot]


def _seed_player_gameweek_stats(db_session, season_id, gameweek_id) -> None:
    for slot, points in _POINTS_BY_SLOT.items():
        db_session.add(
            PlayerGameweekStats(
                player_id=_player_id(db_session, season_id, slot),
                season_id=season_id,
                gameweek_id=gameweek_id,
                minutes=90,
                total_points=points,
                data_source="test",
            )
        )
    db_session.commit()


def _picks_json(db_session, season_id, starter_slots, bench_slots, captain_slot) -> dict:
    picks = []
    for position, slot in enumerate(starter_slots, start=1):
        picks.append(
            {
                "element": _player_fpl_element_id(slot),
                "position": position,
                "multiplier": 2 if slot == captain_slot else 1,
                "is_captain": slot == captain_slot,
                "is_vice_captain": False,
            }
        )
    for offset, slot in enumerate(bench_slots):
        picks.append(
            {
                "element": _player_fpl_element_id(slot),
                "position": 12 + offset,
                "multiplier": 0,
                "is_captain": False,
                "is_vice_captain": False,
            }
        )
    return {"active_chip": None, "automatic_subs": [], "picks": picks}


def _create_manager_sync_snapshot(
    db_session, season_id, gameweek_id, fpl_manager_id, picks_json, **overrides
) -> ManagerSquadSnapshot:
    defaults: dict = dict(
        season_id=season_id,
        source=ManagerSquadSnapshotSource.MANAGER_SYNC,
        fpl_manager_id=fpl_manager_id,
        job_id=None,
        as_of_gameweek_id=gameweek_id,
        bank=10,
        free_transfers_available=1,
        active_chip=None,
        chips_used_json=None,
        overall_rank=None,
        total_points=None,
        squad_player_ids=[],
        squad_player_purchase_prices_json={},
        squad_player_selling_prices_json={},
        estimated_purchase_price_player_ids_json=[],
        unresolved_fpl_element_ids_json=[],
        has_unresolved_players=False,
        raw_entry_json=None,
        raw_history_json=None,
        raw_picks_json=picks_json,
        raw_transfers_json=None,
        is_current=False,
        superseded_at=None,
    )
    defaults.update(overrides)
    snapshot = ManagerSquadSnapshot(**defaults)
    db_session.add(snapshot)
    db_session.commit()
    db_session.refresh(snapshot)
    return snapshot


def _seed_rules_config_id(db_session, season_id) -> uuid.UUID:
    return db_session.execute(
        select(RulesConfig.id).where(RulesConfig.season_id == season_id, RulesConfig.is_current.is_(True))
    ).scalar_one()


def _target_gameweek(db_session, season_id, fpl_event_id: int = 20) -> Gameweek:
    return db_session.execute(
        select(Gameweek).where(Gameweek.season_id == season_id, Gameweek.fpl_event_id == fpl_event_id)
    ).scalar_one()


class _Scenario:
    """Bag of every id ``ingest_outcome_for_audit`` needs, built directly via the ORM (see module
    docstring for why this bypasses the real job handlers).
    """

    def __init__(self):
        self.season_id: uuid.UUID
        self.gameweek_id: uuid.UUID
        self.target_fpl_event_id: int
        self.optimization_run_id: uuid.UUID
        self.transfer_optimization_run_id: uuid.UUID
        self.chip_optimization_run_id: uuid.UUID
        self.h2h_simulation_run_id: uuid.UUID
        self.actual_snapshot: ManagerSquadSnapshot


def _build_full_scenario(db_session) -> _Scenario:
    scenario = _Scenario()
    season_id = _seed_players(db_session)
    scenario.season_id = season_id
    gameweek = _target_gameweek(db_session, season_id)
    scenario.gameweek_id = gameweek.id
    scenario.target_fpl_event_id = gameweek.fpl_event_id
    rules_config_id = _seed_rules_config_id(db_session, season_id)

    user_fpl_manager_id = next(_manager_id_counter)
    rival_fpl_manager_id = next(_manager_id_counter)

    recommended_starters = ["P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9", "P10", "P11"]
    recommended_bench = ["P13", "P14", "P15", "P16"]
    actual_starters = ["P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9", "P10", "P12"]
    actual_bench = ["P13", "P14", "P15", "P16"]

    # The manager's squad state going into this gameweek's decisions -- referenced by all 3
    # planning-time component runs below, and also the H2H "base starting XI" (see
    # app.learning.outcome_ingestion._build_h2h_strategy_comparisons's own docstring).
    snapshot_before = _create_manager_sync_snapshot(
        db_session,
        season_id,
        gameweek.id,
        user_fpl_manager_id,
        _picks_json(db_session, season_id, recommended_starters, recommended_bench, "P1"),
    )

    # The real, post-gameweek sync -- what the user actually played, with a different captain and
    # one different starter, plus a real chip play and 2 real transfers this gameweek.
    actual_snapshot = _create_manager_sync_snapshot(
        db_session,
        season_id,
        gameweek.id,
        user_fpl_manager_id,
        _picks_json(db_session, season_id, actual_starters, actual_bench, "P2"),
        active_chip=ChipType.BBOOST.value,
        raw_history_json={"current": [{"event": gameweek.fpl_event_id, "event_transfers_cost": 4}]},
        raw_transfers_json=[
            {"event": gameweek.fpl_event_id, "element_in": 30021, "element_out": 30020},
            {"event": gameweek.fpl_event_id, "element_in": 30023, "element_out": 30022},
        ],
    )
    scenario.actual_snapshot = actual_snapshot

    _seed_player_gameweek_stats(db_session, season_id, gameweek.id)

    # --- OptimizationRun (recommended squad/XI/captain) ---
    opt_run = OptimizationRun(
        season_id=season_id,
        rules_config_id=rules_config_id,
        recommendation_type=RecommendationType.JOINT_SQUAD_AND_XI,
        run_status=OptimizationRunStatus.SUCCEEDED,
        solver_status=SolverStatus.OPTIMAL,
        target_gameweek_id=gameweek.id,
        target_gameweek_status=TargetGameweekStatus.RESOLVED,
        squad_player_ids=[
            str(_player_id(db_session, season_id, s)) for s in recommended_starters + recommended_bench
        ],
        starting_xi_player_ids=[str(_player_id(db_session, season_id, s)) for s in recommended_starters],
        bench_player_ids_ordered=[str(_player_id(db_session, season_id, s)) for s in recommended_bench],
        captain_player_id=_player_id(db_session, season_id, "P1"),
        vice_captain_player_id=_player_id(db_session, season_id, "P2"),
        squad_total_cost=800,
        squad_projected_points=Decimal("70.00"),
        xi_projected_points=Decimal("65.00"),
        optimization_permitted_snapshot=True,
        data_quality_score_snapshot=Decimal("100.00"),
        rules_status_snapshot=RulesStatus.OK,
        projection_formula_version=1,
        started_at=_now(),
        completed_at=_now(),
    )
    db_session.add(opt_run)
    db_session.commit()
    db_session.refresh(opt_run)
    scenario.optimization_run_id = opt_run.id

    # --- TransferOptimizationRun + 3 scenarios + moves ---
    transfer_run = TransferOptimizationRun(
        manager_squad_snapshot_id=snapshot_before.id,
        season_id=season_id,
        rules_config_id=rules_config_id,
        run_status=TransferOptimizationRunStatus.SUCCEEDED,
        horizon_start_gameweek_id=gameweek.id,
        horizon_length=1,
        horizon_gameweek_ids_json=[str(gameweek.id)],
        free_transfers_available_snapshot=1,
        bank_snapshot=10,
        transfer_hit_cost_snapshot=4,
        recommended_transfers_count=2,
        recommended_hit_cost=4,
        recommended_net_horizon_points_gain=Decimal("6.00"),
        optimization_permitted_snapshot=True,
        data_quality_score_snapshot=Decimal("100.00"),
        rules_status_snapshot=RulesStatus.OK,
        projection_formula_version=1,
        started_at=_now(),
        completed_at=_now(),
    )
    db_session.add(transfer_run)
    db_session.commit()
    db_session.refresh(transfer_run)
    scenario.transfer_optimization_run_id = transfer_run.id

    roll_scenario = TransferOptimizationRunScenario(
        transfer_optimization_run_id=transfer_run.id,
        transfers_count=0,
        raw_horizon_points_gain=Decimal("0.00"),
        hit_cost=0,
        net_horizon_points_gain=Decimal("0.00"),
        is_recommended=False,
    )
    one_transfer_scenario = TransferOptimizationRunScenario(
        transfer_optimization_run_id=transfer_run.id,
        transfers_count=1,
        raw_horizon_points_gain=Decimal("5.00"),
        hit_cost=0,
        net_horizon_points_gain=Decimal("5.00"),
        is_recommended=False,
    )
    two_transfer_scenario = TransferOptimizationRunScenario(
        transfer_optimization_run_id=transfer_run.id,
        transfers_count=2,
        raw_horizon_points_gain=Decimal("10.00"),
        hit_cost=4,
        net_horizon_points_gain=Decimal("6.00"),
        is_recommended=True,
    )
    db_session.add_all([roll_scenario, one_transfer_scenario, two_transfer_scenario])
    db_session.commit()
    for s in (roll_scenario, one_transfer_scenario, two_transfer_scenario):
        db_session.refresh(s)

    db_session.add(
        TransferOptimizationRunMove(
            transfer_optimization_run_scenario_id=one_transfer_scenario.id,
            move_index=0,
            player_out_id=_player_id(db_session, season_id, "P20"),
            player_out_selling_price_snapshot=50,
            player_in_id=_player_id(db_session, season_id, "P21"),
            player_in_cost_snapshot=50,
            projected_points_gain_over_horizon=Decimal("5.00"),
        )
    )
    db_session.add(
        TransferOptimizationRunMove(
            transfer_optimization_run_scenario_id=two_transfer_scenario.id,
            move_index=0,
            player_out_id=_player_id(db_session, season_id, "P20"),
            player_out_selling_price_snapshot=50,
            player_in_id=_player_id(db_session, season_id, "P21"),
            player_in_cost_snapshot=50,
            projected_points_gain_over_horizon=Decimal("5.00"),
        )
    )
    db_session.add(
        TransferOptimizationRunMove(
            transfer_optimization_run_scenario_id=two_transfer_scenario.id,
            move_index=1,
            player_out_id=_player_id(db_session, season_id, "P22"),
            player_out_selling_price_snapshot=50,
            player_in_id=_player_id(db_session, season_id, "P23"),
            player_in_cost_snapshot=50,
            projected_points_gain_over_horizon=Decimal("5.00"),
        )
    )
    db_session.commit()

    # --- ChipOptimizationRun: BBOOST recommended USE_NOW for exactly this gameweek ---
    chip_run = ChipOptimizationRun(
        manager_squad_snapshot_id=snapshot_before.id,
        season_id=season_id,
        rules_config_id=rules_config_id,
        run_status=ChipOptimizationRunStatus.SUCCEEDED,
        target_fpl_event_id=gameweek.fpl_event_id,
        projection_formula_version=1,
        rules_status_snapshot=RulesStatus.OK,
        optimization_permitted_snapshot=True,
        data_quality_score_snapshot=Decimal("100.00"),
        started_at=_now(),
        completed_at=_now(),
    )
    db_session.add(chip_run)
    db_session.commit()
    db_session.refresh(chip_run)
    scenario.chip_optimization_run_id = chip_run.id

    chip_candidate = ChipWindowCandidate(
        chip_optimization_run_id=chip_run.id,
        chip_type=ChipType.BBOOST,
        window_number=1,
        candidate_fpl_event_id=gameweek.fpl_event_id,
        is_blank_gameweek=False,
        is_double_gameweek=False,
        gain_mean=Decimal("6.00"),
        gain_floor=Decimal("4.00"),
        gain_ceiling=Decimal("8.00"),
        sample_count=None,
        rank_within_chip_type=1,
    )
    db_session.add(chip_candidate)
    db_session.commit()
    db_session.refresh(chip_candidate)

    db_session.add(
        ChipRecommendation(
            chip_optimization_run_id=chip_run.id,
            chip_type=ChipType.BBOOST,
            verdict=ChipVerdict.USE_NOW,
            recommended_candidate_id=chip_candidate.id,
            rationale="test bboost use-now",
        )
    )
    db_session.commit()

    # --- League + RivalProfile + H2HSimulationRun + candidates + H2HMatch + RivalGameweekSnapshot ---
    league = League(
        fpl_league_id=next(_league_id_counter),
        name="Test League",
        league_type=LeagueType.H2H,
        closed=False,
        has_cup=False,
        last_synced_at=_now(),
    )
    db_session.add(league)
    db_session.commit()
    db_session.refresh(league)

    rival_profile = RivalProfile(
        season_id=season_id,
        fpl_manager_id=rival_fpl_manager_id,
        league_id=league.id,
        overall_confidence=RivalStateConfidence.MEDIUM,
        computed_at=_now(),
    )
    db_session.add(rival_profile)
    db_session.commit()
    db_session.refresh(rival_profile)

    h2h_run = H2HSimulationRun(
        manager_squad_snapshot_id=snapshot_before.id,
        rival_profile_id=rival_profile.id,
        run_status=H2HSimulationRunStatus.SUCCEEDED,
        target_fpl_event_id=gameweek.fpl_event_id,
        simulation_method_version=1,
        sample_count=1000,
        confidence=RivalStateConfidence.MEDIUM,
        rules_status_snapshot=RulesStatus.OK,
        optimization_permitted_snapshot=True,
        data_quality_score_snapshot=Decimal("100.00"),
        started_at=_now(),
        completed_at=_now(),
    )
    db_session.add(h2h_run)
    db_session.commit()
    db_session.refresh(h2h_run)
    scenario.h2h_simulation_run_id = h2h_run.id

    def _candidate(label, captain_slot):
        return H2HStrategyCandidate(
            h2h_simulation_run_id=h2h_run.id,
            strategy_label=label,
            captain_player_id=(_player_id(db_session, season_id, captain_slot) if captain_slot else None),
            win_probability=Decimal("0.5000"),
            draw_probability=Decimal("0.2000"),
            loss_probability=Decimal("0.3000"),
            expected_margin=Decimal("1.00"),
            is_recommended=False,
            rationale="test",
        )

    ep_optimal = _candidate(H2HStrategyLabel.EP_OPTIMAL, None)
    low_variance = _candidate(H2HStrategyLabel.LOW_VARIANCE, None)
    high_upside = _candidate(H2HStrategyLabel.HIGH_UPSIDE, "P4")
    differential_captain = _candidate(H2HStrategyLabel.DIFFERENTIAL_CAPTAIN, "P3")
    differential_captain.is_recommended = True
    db_session.add_all([ep_optimal, low_variance, high_upside, differential_captain])
    db_session.commit()
    for c in (ep_optimal, low_variance, high_upside, differential_captain):
        db_session.refresh(c)

    h2h_run.recommended_candidate_id = differential_captain.id
    h2h_run.h2h_adjustment_materially_changed = True
    db_session.commit()

    h2h_match = H2HMatch(
        fpl_match_id=next(_h2h_match_id_counter),
        league_id=league.id,
        event=gameweek.fpl_event_id,
        entry_1_id=user_fpl_manager_id,
        entry_2_id=rival_fpl_manager_id,
        entry_1_points=72,
        entry_2_points=65,
        entry_1_win=1,
        entry_1_draw=0,
        entry_1_loss=0,
        entry_2_win=0,
        entry_2_draw=0,
        entry_2_loss=1,
        winner=user_fpl_manager_id,
        is_knockout=False,
        knockout_name=None,
        is_bye=False,
        seed_value=None,
        raw_match_json=None,
        last_synced_at=_now(),
    )
    db_session.add(h2h_match)

    db_session.add(
        RivalGameweekSnapshot(
            fpl_manager_id=rival_fpl_manager_id,
            fpl_event_id=gameweek.fpl_event_id,
            as_of=_now(),
            picks_availability=PicksAvailability.AVAILABLE,
            http_status=200,
            active_chip=None,
            captain_element_id=_player_fpl_element_id("P6"),
            vice_captain_element_id=_player_fpl_element_id("P7"),
            squad_player_ids=[str(_player_id(db_session, season_id, s)) for s in recommended_starters],
            unresolved_fpl_element_ids_json=[],
            event_points=65,
            event_transfers=0,
            event_transfers_cost=0,
            overall_rank=None,
            raw_picks_json=None,
        )
    )
    db_session.commit()

    return scenario


def _create_audit(db_session, scenario: _Scenario, **fk_overrides) -> RecommendationAudit:
    fks = dict(
        optimization_run_id=scenario.optimization_run_id,
        transfer_optimization_run_id=scenario.transfer_optimization_run_id,
        chip_optimization_run_id=scenario.chip_optimization_run_id,
        h2h_simulation_run_id=scenario.h2h_simulation_run_id,
    )
    fks.update(fk_overrides)
    audit = RecommendationAudit(
        season_id=scenario.season_id,
        target_gameweek_id=scenario.gameweek_id,
        target_fpl_event_id=scenario.target_fpl_event_id,
        is_authoritative=True,
        confidence=RecommendationConfidence.HIGH,
        confidence_drivers_json=[],
        assumptions_json=[],
        known_risks_json=[],
        user_actual_decision_json=None,
        generated_at=_now(),
        **fks,
    )
    db_session.add(audit)
    db_session.commit()
    db_session.refresh(audit)
    return audit


def _results_by_type(db_session, outcome_id) -> dict[CounterfactualComparisonType, CounterfactualResult]:
    rows = (
        db_session.execute(
            select(CounterfactualResult).where(CounterfactualResult.recommendation_outcome_id == outcome_id)
        )
        .scalars()
        .all()
    )
    return {row.comparison_type: row for row in rows}


def test_ingest_outcome_full_happy_path_computes_all_eight_comparisons(
    db_session, patched_fpl_client_optimization
):
    scenario = _build_full_scenario(db_session)
    audit = _create_audit(db_session, scenario)

    outcome = ingest_outcome_for_audit(db_session, audit.id)

    assert outcome.recommendation_audit_id == audit.id
    assert outcome.actual_xi_points == 72
    assert outcome.actual_captain_points == 16
    assert outcome.actual_bench_points == 6
    assert outcome.actual_chip_played == "bboost"
    assert outcome.actual_hit_cost_taken == 4
    assert outcome.actual_transfers_made_json is not None
    assert len(outcome.actual_transfers_made_json) == 2
    assert outcome.h2h_result == "win"
    assert outcome.h2h_match_id is not None
    assert outcome.opponent_actual_captain_player_id == _player_fpl_element_id("P6")
    assert outcome.recommendation_net_gain == Decimal("6.00")
    assert outcome.finalized_at is not None

    results = _results_by_type(db_session, outcome.id)
    assert set(results) == set(CounterfactualComparisonType)

    def assert_result(comparison_type, baseline, alternative, delta, is_value_adding):
        row = results[comparison_type]
        assert row.baseline_points == Decimal(str(baseline))
        assert row.alternative_points == Decimal(str(alternative))
        assert row.delta == Decimal(str(delta))
        assert row.is_value_adding is is_value_adding

    assert_result(CounterfactualComparisonType.RECOMMENDED_VS_ACTUAL_TEAM, 72, 71, -1, False)
    assert_result(CounterfactualComparisonType.RECOMMENDED_VS_ACTUAL_CAPTAIN, 16, 20, 4, True)
    assert_result(CounterfactualComparisonType.TRANSFER_VS_NO_TRANSFER, 0, 6, 6, True)
    assert_result(CounterfactualComparisonType.TRANSFER_VS_TOP_ALTERNATIVE, 5, 6, 1, True)
    assert_result(CounterfactualComparisonType.HIT_VS_NO_HIT, 5, 6, 1, True)
    assert_result(CounterfactualComparisonType.CHIP_VS_HOLD, 0, 6, 6, True)
    assert_result(CounterfactualComparisonType.OVERALL_VS_H2H_ADJUSTED, 71, 66, -5, False)
    assert_result(CounterfactualComparisonType.CONSERVATIVE_VS_AGGRESSIVE, 71, 66, -5, False)


def test_ingest_outcome_partial_availability_only_computes_comparisons_with_resolvable_components(
    db_session, patched_fpl_client_optimization
):
    scenario = _build_full_scenario(db_session)
    # Only optimization_run_id + transfer_optimization_run_id present -- no chip/H2H component runs
    # at all (a realistic partial-availability gap, e.g. chips not evaluated yet this gameweek, no
    # H2H opponent yet).
    audit = _create_audit(
        db_session,
        scenario,
        chip_optimization_run_id=None,
        h2h_simulation_run_id=None,
    )

    outcome = ingest_outcome_for_audit(db_session, audit.id)

    # The "actual" snapshot is still resolvable (via transfer_optimization_run_id, checked first --
    # see _resolve_relevant_manager_squad_snapshot's own priority order), so team/captain scalars and
    # comparisons are still computed even with chip/H2H both absent.
    assert outcome.actual_xi_points == 72
    assert outcome.actual_captain_points == 16
    # actual_chip_played is still reported (the actual snapshot itself carries it) even though there
    # is no ChipOptimizationRun to compare against -- see _build_chip_comparison's own docstring.
    assert outcome.actual_chip_played == "bboost"
    # No H2H component run at all -- h2h fields stay unresolved, never fabricated.
    assert outcome.h2h_match_id is None
    assert outcome.h2h_result is None
    assert outcome.opponent_actual_squad_player_ids_json is None
    assert outcome.opponent_actual_captain_player_id is None

    results = _results_by_type(db_session, outcome.id)
    assert set(results) == {
        CounterfactualComparisonType.RECOMMENDED_VS_ACTUAL_TEAM,
        CounterfactualComparisonType.RECOMMENDED_VS_ACTUAL_CAPTAIN,
        CounterfactualComparisonType.TRANSFER_VS_NO_TRANSFER,
        CounterfactualComparisonType.TRANSFER_VS_TOP_ALTERNATIVE,
        CounterfactualComparisonType.HIT_VS_NO_HIT,
    }


def test_ingest_outcome_no_manager_snapshot_resolvable_computes_only_transfer_comparisons(
    db_session, patched_fpl_client_optimization
):
    # Only transfer_optimization_run_id present, and its own manager_squad_snapshot_id points at a
    # MANUAL_ENTRY-style squad with no real fpl_manager_id -- team/captain comparisons and every
    # actual_* scalar are correctly skipped (never fabricated), but the transfer comparisons (which
    # never need a real manager to resolve) still compute.
    scenario = _build_full_scenario(db_session)
    manual_snapshot = _create_manager_sync_snapshot(
        db_session,
        scenario.season_id,
        scenario.gameweek_id,
        None,
        _picks_json(db_session, scenario.season_id, ["P1", "P2"], [], "P1"),
        source=ManagerSquadSnapshotSource.MANUAL_ENTRY,
        as_of_gameweek_id=None,
    )
    transfer_run = db_session.get(TransferOptimizationRun, scenario.transfer_optimization_run_id)
    transfer_run.manager_squad_snapshot_id = manual_snapshot.id
    db_session.commit()

    audit = _create_audit(
        db_session,
        scenario,
        optimization_run_id=None,
        chip_optimization_run_id=None,
        h2h_simulation_run_id=None,
    )

    outcome = ingest_outcome_for_audit(db_session, audit.id)

    assert outcome.actual_xi_points is None
    assert outcome.actual_captain_points is None
    assert outcome.actual_bench_points is None
    assert outcome.actual_chip_played is None

    results = _results_by_type(db_session, outcome.id)
    assert set(results) == {
        CounterfactualComparisonType.TRANSFER_VS_NO_TRANSFER,
        CounterfactualComparisonType.TRANSFER_VS_TOP_ALTERNATIVE,
        CounterfactualComparisonType.HIT_VS_NO_HIT,
    }


def test_ingest_outcome_gameweek_not_finished_raises_and_persists_nothing(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    ingestion_job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert ingestion_job.status == JobStatus.SUCCEEDED
    season_id = db_session.execute(
        select(Season.id).where(Season.label == ingestion_job.output_json["season_label"])
    ).scalar_one()
    unfinished_gameweek = db_session.execute(
        select(Gameweek).where(Gameweek.season_id == season_id, Gameweek.finished.is_(False))
    ).scalar_one()

    audit = RecommendationAudit(
        season_id=season_id,
        target_gameweek_id=unfinished_gameweek.id,
        target_fpl_event_id=unfinished_gameweek.fpl_event_id,
        is_authoritative=True,
        confidence=RecommendationConfidence.LOW,
        confidence_drivers_json=[],
        assumptions_json=[],
        known_risks_json=[],
        generated_at=_now(),
    )
    db_session.add(audit)
    db_session.commit()
    db_session.refresh(audit)

    try:
        ingest_outcome_for_audit(db_session, audit.id)
        raise AssertionError("expected GameweekNotFinishedError")
    except GameweekNotFinishedError:
        pass

    assert (
        db_session.execute(
            select(RecommendationOutcome).where(RecommendationOutcome.recommendation_audit_id == audit.id)
        ).scalar_one_or_none()
        is None
    )


def test_ingest_outcome_unknown_audit_id_raises_not_found(db_session):
    try:
        ingest_outcome_for_audit(db_session, uuid.uuid4())
        raise AssertionError("expected RecommendationAuditNotFoundError")
    except RecommendationAuditNotFoundError:
        pass


def test_ingest_outcome_rerun_upserts_in_place_rather_than_duplicating(
    db_session, patched_fpl_client_optimization
):
    scenario = _build_full_scenario(db_session)
    audit = _create_audit(db_session, scenario)

    first_outcome = ingest_outcome_for_audit(db_session, audit.id)
    first_outcome_id = first_outcome.id
    first_finalized_at = first_outcome.finalized_at

    second_outcome = ingest_outcome_for_audit(db_session, audit.id)

    assert second_outcome.id == first_outcome_id
    assert second_outcome.finalized_at >= first_finalized_at

    all_outcomes = (
        db_session.execute(
            select(RecommendationOutcome).where(RecommendationOutcome.recommendation_audit_id == audit.id)
        )
        .scalars()
        .all()
    )
    assert len(all_outcomes) == 1

    results = _results_by_type(db_session, second_outcome.id)
    assert set(results) == set(CounterfactualComparisonType)
    assert (
        len(
            db_session.execute(
                select(CounterfactualResult).where(
                    CounterfactualResult.recommendation_outcome_id == second_outcome.id
                )
            )
            .scalars()
            .all()
        )
        == 8
    )


def test_ingest_outcome_rerun_reflects_corrected_player_gameweek_stats(
    db_session, patched_fpl_client_optimization
):
    # A late bonus-points correction after the first ingestion -- see RecommendationOutcome's own
    # docstring for why upsert-in-place (not refuse/accumulate) was the deliberate choice here.
    scenario = _build_full_scenario(db_session)
    audit = _create_audit(db_session, scenario)
    ingest_outcome_for_audit(db_session, audit.id)

    p1_id = _player_id(db_session, scenario.season_id, "P1")
    row = db_session.execute(
        select(PlayerGameweekStats).where(
            PlayerGameweekStats.player_id == p1_id, PlayerGameweekStats.gameweek_id == scenario.gameweek_id
        )
    ).scalar_one()
    row.total_points = 20  # was 10 -- a late bonus-points correction
    db_session.commit()

    outcome = ingest_outcome_for_audit(db_session, audit.id)

    # Recommended captain (P1) raw points went from 10 to 20 -- captain comparison should now be
    # baseline=16 (unchanged, actual captain P2 raw=8*2), alternative=40 (20*2), delta=24.
    results = _results_by_type(db_session, outcome.id)
    captain_result = results[CounterfactualComparisonType.RECOMMENDED_VS_ACTUAL_CAPTAIN]
    assert captain_result.alternative_points == Decimal("40.00")
    assert captain_result.delta == Decimal("24.00")
