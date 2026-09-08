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
with a real ``raw_picks_json`` (11 starters + 4 bench, one captain, one
vice-captain) drawn from the same ~60-player pool -- mirrors
``test_transfer_optimization_job.py``'s own "hand-verifiable manual snapshot"
discipline, but this job additionally NEEDS ``raw_picks_json`` (not just
``squad_player_ids``) to resolve a starting XI/captain at all (see
``app.jobs.handlers.h2h_simulation``'s own docstring).

Two of the default squad's 11 starters (elements 30017/30034, see
``build_optimization_bootstrap_json``'s ``index % 17 == 0`` rule) are
deliberately HARD-EXCLUDED (status ``"i"``) in the synthetic pool -- this is
not an oversight, it's what exercises the real-FPL-style autosub path
(``_resolve_effective_starting_xi``) even in the "happy path" test, since a
real squad legitimately has injured players some weeks. Score assertions
that depend on autosub having actually happened use a wide INEQUALITY
threshold (comfortably separating "autosub happened" from "it didn't"),
never a tight closeness check against an exact float -- ``player_gameweek_stats``
residual pooling is now cross-season (see ``_ResidualCache``'s own
docstring), so a persistent, repeatedly-rerun local Postgres can
legitimately accumulate extra historical residual data across suite
invocations that would make an exact-value assertion flaky.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.jobs.service import execute_job
from packages.database.models.gameweek import Gameweek
from packages.database.models.h2h_simulation_run import H2HSimulationRun
from packages.database.models.h2h_strategy_candidate import H2HStrategyCandidate
from packages.database.models.ingestion_run import IngestionRun
from packages.database.models.manager_squad_snapshot import ManagerSquadSnapshot
from packages.database.models.player import Player
from packages.database.models.player_gameweek_stats import PlayerGameweekStats
from packages.database.models.position import Position
from packages.database.models.rival_profile import RivalProfile
from packages.database.models.rules_config import RulesConfig
from packages.database.models.season import Season
from packages.database.models.transfer_optimization_run_scenario import TransferOptimizationRunScenario
from packages.domain.h2h import H2HSimulationRunStatus, H2HStrategyLabel, RivalStateConfidence
from packages.domain.jobs import JobStatus, JobType, TriggerType
from packages.domain.rules import RulesStatus
from packages.domain.transfers import ManagerSquadSnapshotSource
from tests.integration.conftest import (
    LEAGUE_RIVAL_B_FPL_MANAGER_ID,
    LEAGUE_RIVAL_C_FPL_MANAGER_ID,
    LEAGUE_SELF_FPL_MANAGER_ID,
)

# --- Milestone 5 Layer 4 (H2H Strategy): a hand-verified 15-player squad, IDENTICAL to
# test_transfer_optimization_job.py's own hand-verified squad (same element ids/bank/free
# transfers), reused here so its already-proven 1-/2-transfer gain numbers
# (1-transfer: OUT 30011 -> IN 30021, gain=5.5; 2-transfer: OUT {30001, 30011} -> IN {30009,
# 30021}, gain=10.0, hit_cost=0 given free_transfers_available=2) hold without re-deriving
# them by hand a second time -- but ALSO given a real raw_picks_json (11 starters + 4 bench,
# captain/vice-captain) since, unlike TRANSFER_OPTIMIZATION, H2H_SIMULATION needs a resolved
# starting XI. Captain = 30010 (DEF, ep_next=8.5) -- the single highest-projected STARTER,
# with no residual variance/appearance-risk data recorded for it (status "a", no
# player-specific or DEF-position player_gameweek_stats seeded anywhere in this suite) -- so
# LOW_VARIANCE and CAPTAIN_COVER are BOTH guaranteed to re-pick this exact same player
# regardless of any MID-position residual contamination this same persistent Postgres may
# have accumulated from test_h2h_simulation_residual_bootstrap_uses_cross_season_position_pool_
# when_current_season_is_cold's own seeded data elsewhere in this file (that seed only ever
# touches the MID pool, and 30010 is DEF and the outright highest raw projection among
# starters, so it can never be beaten by a variance-boosted MID competitor for either of those
# two functions -- see this module's own reasoning in the PR description / commit message).
_STRATEGY_SQUAD_ELEMENT_IDS = [
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
_STRATEGY_STARTING_ELEMENTS = [30001, 30010, 30011, 30019, 30014, 30028, 30029, 30030, 30031, 30048, 30049]
_STRATEGY_BENCH_ELEMENTS = [30002, 30016, 30032, 30050]
_STRATEGY_CAPTAIN_ELEMENT = 30010
_STRATEGY_VICE_CAPTAIN_ELEMENT = 30031
_STRATEGY_BANK = 20
_STRATEGY_FREE_TRANSFERS_AVAILABLE = 2

_ALL_STRATEGY_LABELS = frozenset(H2HStrategyLabel)
_NO_TRANSFER_STRATEGY_LABEL_VALUES = _ALL_STRATEGY_LABELS - {
    H2HStrategyLabel.NO_HIT,
    H2HStrategyLabel.CALCULATED_HIT,
}

FPL_LEAGUE_ID = 88891  # distinct from test_league_sync_job.py's 88888/test_rival_profile.py's 88889

# 11 starters (one captained, one vice-captained) + 4 bench, all drawn from
# the ~60-player synthetic optimization pool (element ids 30000-30059, see
# build_optimization_bootstrap_json in conftest.py) -- deliberately disjoint
# from the rival's own squad (picks_b.json's 30000/30010-13/30028-31/30048-50)
# so the two sides are visibly distinct squads. 30017 (DEF) and 30034 (MID)
# are hard-excluded (status "i") in the pool; 30052 (FWD) is doubtful
# (status "d", down-weighted, NOT hard-excluded/autosubbed); every bench
# element (30004 GKP/30019 DEF/30038 MID/30054 FWD) is healthy.
_USER_STARTING_ELEMENTS = [30003, 30015, 30016, 30017, 30018, 30033, 30034, 30035, 30036, 30052, 30053]
_USER_CAPTAIN_ELEMENT = 30033
_USER_VICE_CAPTAIN_ELEMENT = 30035
_USER_BENCH_ELEMENTS = [30004, 30019, 30038, 30054]


def _user_raw_picks_json(
    starting_elements: list[int] = _USER_STARTING_ELEMENTS,
    bench_elements: list[int] = _USER_BENCH_ELEMENTS,
    captain_element: int = _USER_CAPTAIN_ELEMENT,
    vice_captain_element: int = _USER_VICE_CAPTAIN_ELEMENT,
) -> dict:
    picks = []
    for position, element in enumerate(starting_elements, start=1):
        picks.append(
            {
                "element": element,
                "position": position,
                "multiplier": 2 if element == captain_element else 1,
                "is_captain": element == captain_element,
                "is_vice_captain": element == vice_captain_element,
            }
        )
    for offset, element in enumerate(bench_elements):
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


def _seed_cross_season_mid_residuals(db_session) -> None:
    """Seed a handful of ``player_gameweek_stats`` rows for a wholly SEPARATE historical
    season/player sharing the "MID" position's short_name, with a wide (actual - xP) spread.

    Used to verify ``_ResidualCache``'s cross-season fallback: the CURRENT season (freshly
    ingested via DATA_INGESTION in these tests) has zero player_gameweek_stats of its own, so
    without this fallback every MID starter's residual sampling would silently degrade to
    zero-variance noise.
    """
    other_season = Season(label=f"h2h-hist-{uuid.uuid4().hex[:8]}", is_current=False)  # String(20) max
    db_session.add(other_season)
    db_session.flush()

    position = Position(
        fpl_element_type_id=3,
        season_id=other_season.id,
        singular_name="Midfielder",
        short_name="MID",
        squad_select=5,
        squad_min_play=2,
        squad_max_play=5,
    )
    db_session.add(position)
    db_session.flush()

    other_player = Player(
        fpl_element_id=999001,
        season_id=other_season.id,
        position_id=position.id,
        now_cost=50,
        last_synced_at=datetime.now(UTC),
    )
    db_session.add(other_player)
    db_session.flush()

    # One row per DISTINCT historical gameweek (never several rows sharing one
    # (player_id, gameweek_id) pair differentiated only by a NULL fixture_id --
    # that's only legal under the CURRENT 3-column unique constraint, and
    # test_migrations.py's own downgrade cycle reverts to an older 2-column
    # (player_id, gameweek_id) constraint that would reject it). Wide, clearly
    # non-zero residual spread: actual points swing far from xP.
    for event, (total_points, xp_estimate) in enumerate(
        [(2, 6.0), (14, 6.0), (1, 6.0), (12, 6.0), (7, 6.0)], start=1
    ):
        gameweek = Gameweek(
            fpl_event_id=event,
            season_id=other_season.id,
            name=f"Historical GW{event}",
            deadline_time=datetime.now(UTC),
            finished=True,
            last_synced_at=datetime.now(UTC),
        )
        db_session.add(gameweek)
        db_session.flush()
        db_session.add(
            PlayerGameweekStats(
                player_id=other_player.id,
                season_id=other_season.id,
                gameweek_id=gameweek.id,
                total_points=total_points,
                xp_estimate=xp_estimate,
                data_source="test",
            )
        )
    db_session.commit()


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


def _create_manual_user_snapshot(
    db_session,
    season_id,
    raw_picks_json: dict | None,
    squad_elements: list[int] | None = None,
    bank: int = 5,
    free_transfers_available: int = 1,
) -> ManagerSquadSnapshot:
    all_elements = (
        squad_elements if squad_elements is not None else _USER_STARTING_ELEMENTS + _USER_BENCH_ELEMENTS
    )
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
        bank=bank,
        free_transfers_available=free_transfers_available,
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


def _run_transfer_optimization(db_session, snapshot_id, horizon_gameweeks: int = 6):
    return execute_job(
        db_session,
        job_type=JobType.TRANSFER_OPTIMIZATION,
        parameters={"manager_squad_snapshot_id": str(snapshot_id), "horizon_gameweeks": horizon_gameweeks},
        requested_by="integration-test",
        trigger_type=TriggerType.LOCAL,
    )


def _strategy_candidates_by_label(
    db_session, h2h_simulation_run_id
) -> dict[H2HStrategyLabel, H2HStrategyCandidate]:
    rows = (
        db_session.execute(
            select(H2HStrategyCandidate).where(
                H2HStrategyCandidate.h2h_simulation_run_id == h2h_simulation_run_id
            )
        )
        .scalars()
        .all()
    )
    return {row.strategy_label: row for row in rows}


def test_h2h_simulation_full_strategy_comparison_persists_all_seven_candidates_with_prior_transfer_run(
    db_session, patched_fpl_client_optimization_with_active_gameweek, patched_fpl_client_league
):
    """Milestone 5 Layer 4 (H2H Strategy) end-to-end: a real, prior SUCCEEDED
    ``TransferOptimizationRun`` exists for this exact snapshot (run first, below), so all 7 named
    candidates should be built, simulated, and persisted -- including ``NO_HIT``/``CALCULATED_HIT``,
    which cite that run's own 0-transfer/2-transfer scenarios as provenance.

    Also proves the scores-cache/determinism fix (``h2h_simulation.py``'s
    ``user_scores_by_squad`` dict): ``CAPTAIN_COVER`` and ``LOW_VARIANCE`` are both guaranteed (see
    this module's own squad-construction docstring above) to re-pick the SAME captain (30010) as
    ``EP_OPTIMAL`` already has -- an IDENTICAL resulting squad, not merely a similar one -- so their
    persisted win/draw/loss/margin must be EXACTLY equal to ``EP_OPTIMAL``'s, not independently
    re-sampled near-duplicates that Monte Carlo noise could have pulled apart.
    """
    season_id = _seed_players_with_active_gameweek(db_session)
    league_job = _run_league_sync(db_session)
    assert league_job.status == JobStatus.SUCCEEDED
    rival_profile = _rival_profile(db_session, season_id, LEAGUE_RIVAL_B_FPL_MANAGER_ID)

    picks_json = _user_raw_picks_json(
        starting_elements=_STRATEGY_STARTING_ELEMENTS,
        bench_elements=_STRATEGY_BENCH_ELEMENTS,
        captain_element=_STRATEGY_CAPTAIN_ELEMENT,
        vice_captain_element=_STRATEGY_VICE_CAPTAIN_ELEMENT,
    )
    snapshot = _create_manual_user_snapshot(
        db_session,
        season_id,
        picks_json,
        squad_elements=_STRATEGY_SQUAD_ELEMENT_IDS,
        bank=_STRATEGY_BANK,
        free_transfers_available=_STRATEGY_FREE_TRANSFERS_AVAILABLE,
    )

    transfer_job = _run_transfer_optimization(db_session, snapshot.id)
    assert transfer_job.status == JobStatus.SUCCEEDED
    # Hand-verified (identical squad/bank/free-transfers to test_transfer_optimization_job.py's
    # own proven case): the 2-transfer scenario (gain=10.0, hit_cost=0) strictly beats both the
    # 0- and 1-transfer scenarios, so CALCULATED_HIT is generated (not skipped as a roll).
    assert transfer_job.output_json["recommended_transfers_count"] == 2
    assert transfer_job.output_json["recommended_hit_cost"] == 0
    transfer_run_id = uuid.UUID(transfer_job.output_json["transfer_optimization_run_id"])
    scenario_0 = db_session.execute(
        select(TransferOptimizationRunScenario).where(
            TransferOptimizationRunScenario.transfer_optimization_run_id == transfer_run_id,
            TransferOptimizationRunScenario.transfers_count == 0,
        )
    ).scalar_one()
    scenario_2 = db_session.execute(
        select(TransferOptimizationRunScenario).where(
            TransferOptimizationRunScenario.transfer_optimization_run_id == transfer_run_id,
            TransferOptimizationRunScenario.transfers_count == 2,
        )
    ).scalar_one()

    job = _run_h2h_simulation(db_session, snapshot.id, rival_profile.id)

    assert job.status == JobStatus.SUCCEEDED
    run_id = uuid.UUID(job.output_json["h2h_simulation_run_id"])
    run_row = db_session.get(H2HSimulationRun, run_id)
    assert run_row is not None

    candidates = _strategy_candidates_by_label(db_session, run_id)
    assert set(candidates.keys()) == _ALL_STRATEGY_LABELS  # all 7, none skipped

    # Exactly one row is_recommended, and the run's own recommended_candidate_id points at it.
    recommended_rows = [row for row in candidates.values() if row.is_recommended]
    assert len(recommended_rows) == 1
    recommended_row = recommended_rows[0]
    assert run_row.recommended_candidate_id == recommended_row.id

    # The master spec's own required disclosure: materially_changed is True exactly when the
    # recommendation isn't simply "do nothing different" -- a genuine end-to-end wiring check
    # (app.h2h.strategy.h2h_adjustment_materially_changed is already unit-tested in isolation;
    # this re-derives the same fact from real persisted rows).
    assert run_row.h2h_adjustment_materially_changed == (
        recommended_row.strategy_label != H2HStrategyLabel.EP_OPTIMAL
    )

    # NO_HIT/CALCULATED_HIT cite the real Milestone 4 scenario rows as provenance; every other
    # candidate never fabricates one.
    assert candidates[H2HStrategyLabel.NO_HIT].transfer_optimization_run_scenario_id == scenario_0.id
    assert candidates[H2HStrategyLabel.CALCULATED_HIT].transfer_optimization_run_scenario_id == scenario_2.id
    for label in _NO_TRANSFER_STRATEGY_LABEL_VALUES:
        assert candidates[label].transfer_optimization_run_scenario_id is None

    # EP_OPTIMAL's own row never carries a captain_player_id (nothing to compare itself against).
    assert candidates[H2HStrategyLabel.EP_OPTIMAL].captain_player_id is None

    # CALCULATED_HIT's own squad is genuinely, deterministically better (a real +10.0 projected-
    # points swing from 2 clean, non-MID transfers shared by no other candidate) -- this holds
    # regardless of any residual noise/contamination, since both EP_OPTIMAL and CALCULATED_HIT
    # share every OTHER player at the exact same multiplier, so only the swapped pair's own
    # (noise-free, since neither GKP nor DEF residuals are ever seeded in this suite) projection
    # difference can move the needle.
    ep_optimal_row = candidates[H2HStrategyLabel.EP_OPTIMAL]
    calculated_hit_row = candidates[H2HStrategyLabel.CALCULATED_HIT]
    assert calculated_hit_row.win_probability >= ep_optimal_row.win_probability

    # Scores-cache/determinism proof: CAPTAIN_COVER and LOW_VARIANCE both independently re-derive
    # the SAME captain (30010) EP_OPTIMAL already has -- see this module's squad-construction
    # docstring for why this holds regardless of any cross-test residual contamination -- so their
    # resulting squads are value-identical to EP_OPTIMAL's, and must therefore produce EXACTLY
    # equal persisted results, not independently re-sampled near-duplicates.
    for label in (H2HStrategyLabel.CAPTAIN_COVER, H2HStrategyLabel.LOW_VARIANCE):
        candidate_row = candidates[label]
        assert candidate_row.captain_player_id is None  # same final captain as EP_OPTIMAL
        assert candidate_row.win_probability == ep_optimal_row.win_probability
        assert candidate_row.draw_probability == ep_optimal_row.draw_probability
        assert candidate_row.loss_probability == ep_optimal_row.loss_probability
        assert candidate_row.expected_margin == ep_optimal_row.expected_margin


def test_h2h_simulation_calculated_hit_penalizes_win_probability_for_a_real_transfer_hit(
    db_session, patched_fpl_client_optimization_with_active_gameweek, patched_fpl_client_league
):
    """Real bug this closes (found and fixed 2026-09-07): app.h2h.simulate had no notion of
    transfers/hit costs at all -- a CALCULATED_HIT candidate whose recommended scenario genuinely
    took a real hit had its win probability computed against the FULL, un-penalized squad score.

    Identical squad/transfers, run twice, differing ONLY in ``free_transfers_available`` (2 vs
    1): the same 2-transfer scenario (raw gain 10.0 over the horizon) costs ``hit_cost=0`` when 2
    transfers are free, but a real ``hit_cost=4`` when only 1 is (one transfer beyond the free
    allowance) -- still beating the 1-transfer scenario's own net gain (5.5, hit_cost=0) either
    way (6.0 > 5.5 or 10.0 > 5.5), so CALCULATED_HIT is built and recommended in both cases. A
    correctly-applied real -4 penalty must measurably lower CALCULATED_HIT's win probability
    versus the identical, unpenalized squad -- comparing two real runs, not reaching into any
    implementation internals, so this is real end-to-end behavior, not a mocked assertion.
    """
    season_id = _seed_players_with_active_gameweek(db_session)
    _run_league_sync(db_session)
    rival_profile = _rival_profile(db_session, season_id, LEAGUE_RIVAL_B_FPL_MANAGER_ID)
    picks_json = _user_raw_picks_json(
        starting_elements=_STRATEGY_STARTING_ELEMENTS,
        bench_elements=_STRATEGY_BENCH_ELEMENTS,
        captain_element=_STRATEGY_CAPTAIN_ELEMENT,
        vice_captain_element=_STRATEGY_VICE_CAPTAIN_ELEMENT,
    )

    def _calculated_hit_win_probability(free_transfers_available: int, expected_hit_cost: int) -> Decimal:
        snapshot = _create_manual_user_snapshot(
            db_session,
            season_id,
            picks_json,
            squad_elements=_STRATEGY_SQUAD_ELEMENT_IDS,
            bank=_STRATEGY_BANK,
            free_transfers_available=free_transfers_available,
        )
        transfer_job = _run_transfer_optimization(db_session, snapshot.id)
        assert transfer_job.status == JobStatus.SUCCEEDED
        assert transfer_job.output_json["recommended_transfers_count"] == 2
        assert transfer_job.output_json["recommended_hit_cost"] == expected_hit_cost

        job = _run_h2h_simulation(db_session, snapshot.id, rival_profile.id)
        assert job.status == JobStatus.SUCCEEDED
        run_id = uuid.UUID(job.output_json["h2h_simulation_run_id"])
        candidates = _strategy_candidates_by_label(db_session, run_id)
        assert H2HStrategyLabel.CALCULATED_HIT in candidates
        return candidates[H2HStrategyLabel.CALCULATED_HIT].win_probability

    unpenalized_win_probability = _calculated_hit_win_probability(
        free_transfers_available=2, expected_hit_cost=0
    )
    penalized_win_probability = _calculated_hit_win_probability(
        free_transfers_available=1, expected_hit_cost=4
    )

    assert penalized_win_probability < unpenalized_win_probability


def test_h2h_simulation_strategy_comparison_skips_no_hit_and_calculated_hit_without_a_prior_transfer_run(
    db_session, patched_fpl_client_optimization_with_active_gameweek, patched_fpl_client_league
):
    """No prior ``TransferOptimizationRun`` exists for this snapshot -- exactly 5 candidates
    (``NO_HIT``/``CALCULATED_HIT`` both absent, never fabricated without a real Milestone 4
    scenario row to cite) should be built, simulated, and persisted, with no crash and exactly one
    ``is_recommended``.
    """
    season_id = _seed_players_with_active_gameweek(db_session)
    _run_league_sync(db_session)
    rival_profile = _rival_profile(db_session, season_id, LEAGUE_RIVAL_B_FPL_MANAGER_ID)
    snapshot = _create_manual_user_snapshot(db_session, season_id, _user_raw_picks_json())

    job = _run_h2h_simulation(db_session, snapshot.id, rival_profile.id)

    assert job.status == JobStatus.SUCCEEDED
    run_id = uuid.UUID(job.output_json["h2h_simulation_run_id"])
    run_row = db_session.get(H2HSimulationRun, run_id)
    assert run_row is not None

    candidates = _strategy_candidates_by_label(db_session, run_id)
    assert set(candidates.keys()) == _NO_TRANSFER_STRATEGY_LABEL_VALUES
    assert H2HStrategyLabel.NO_HIT not in candidates
    assert H2HStrategyLabel.CALCULATED_HIT not in candidates

    recommended_rows = [row for row in candidates.values() if row.is_recommended]
    assert len(recommended_rows) == 1
    recommended_row = recommended_rows[0]
    assert run_row.recommended_candidate_id == recommended_row.id
    assert run_row.h2h_adjustment_materially_changed == (
        recommended_row.strategy_label != H2HStrategyLabel.EP_OPTIMAL
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

    # Autosub actually happened for the squad's 2 hard-excluded starters (see this
    # module's own docstring): without it, 30017/30034 would contribute 0 each and
    # the mean would sit around ~49; with their bench replacements (ep_next 7.4/5.8)
    # correctly subbed in, it should sit comfortably higher. Wide inequality, not a
    # tight closeness check -- see module docstring on cross-season residual pooling.
    assert float(run_row.user_score_mean) > 55


def test_h2h_simulation_captain_autosubbed_out_transfers_armband_to_vice_captain(
    db_session, patched_fpl_client_optimization_with_active_gameweek, patched_fpl_client_league
):
    """A minimal, dedicated 3-starter squad (not the shared 11-man fixture) to keep the expected
    score gap between "armband transferred correctly" and "armband lost/mis-assigned" large and
    unambiguous, regardless of any residual-bootstrap noise/bias from other tests' data.

    Captain = 30034 (MID, hard-excluded/injured) -> autosubbed out by 30038 (MID, healthy, never
    inherits the armband). Vice-captain = 30053 (FWD, ep_next=8.0, healthy) -> should inherit the
    2x armband. Filler = 30015 (DEF, ep_next=5.2, untouched). Expected sums: armband correctly
    transferred ~= 5.2 + 8.0*2 + 5.8 = 27.0; armband lost (bug) ~= 5.2 + 8.0 + 5.8 = 19.0; armband
    wrongly given to the sub (bug) ~= 5.2 + 8.0 + 5.8*2 = 24.8. A ">23" threshold cleanly separates
    "transferred" from both bug scenarios.
    """
    season_id = _seed_players_with_active_gameweek(db_session)
    _run_league_sync(db_session)
    rival_profile = _rival_profile(db_session, season_id, LEAGUE_RIVAL_B_FPL_MANAGER_ID)

    starting_elements = [30015, 30034, 30053]  # DEF (healthy), MID (injured, captain), FWD (healthy, vice)
    bench_elements = [30038]  # MID (healthy) -- the only eligible same-class replacement available
    picks_json = _user_raw_picks_json(
        starting_elements=starting_elements,
        bench_elements=bench_elements,
        captain_element=30034,
        vice_captain_element=30053,
    )
    snapshot = _create_manual_user_snapshot(
        db_session, season_id, picks_json, squad_elements=starting_elements + bench_elements
    )

    job = _run_h2h_simulation(db_session, snapshot.id, rival_profile.id)

    assert job.status == JobStatus.SUCCEEDED
    run_row = db_session.get(H2HSimulationRun, uuid.UUID(job.output_json["h2h_simulation_run_id"]))
    assert float(run_row.user_score_mean) > 23.0


def test_h2h_simulation_residual_bootstrap_uses_cross_season_position_pool_when_current_season_is_cold(
    db_session, patched_fpl_client_optimization_with_active_gameweek, patched_fpl_client_league
):
    season_id = _seed_players_with_active_gameweek(db_session)
    _run_league_sync(db_session)
    rival_profile = _rival_profile(db_session, season_id, LEAGUE_RIVAL_B_FPL_MANAGER_ID)
    snapshot = _create_manual_user_snapshot(db_session, season_id, _user_raw_picks_json())

    _seed_cross_season_mid_residuals(db_session)

    job = _run_h2h_simulation(db_session, snapshot.id, rival_profile.id)

    assert job.status == JobStatus.SUCCEEDED
    run_row = db_session.get(H2HSimulationRun, uuid.UUID(job.output_json["h2h_simulation_run_id"]))
    # Non-zero variance now present on the user side -- the current season has zero
    # player_gameweek_stats of its own, so this can only come from the cross-season fallback.
    assert float(run_row.user_score_floor) < float(run_row.user_score_ceiling)


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


def test_h2h_simulation_snapshot_missing_picks_data_falls_back_to_solved_lineup(
    db_session, patched_fpl_client_optimization_with_active_gameweek, patched_fpl_client_league
):
    """A user snapshot with no raw_picks_json (every MANUAL_ENTRY squad -- see that model's own
    docstring) can't have its starting XI read verbatim, but H2H_SIMULATION must not just refuse
    outright: _derive_starting_and_bench_from_solver solves one instead, mirroring
    CHIP_SIMULATION's identical fix (Milestone 8 item 5). The RIVAL side is untouched -- still
    covered by test_h2h_simulation_rival_squad_unavailable_creates_no_row below."""
    season_id = _seed_players_with_active_gameweek(db_session)
    _run_league_sync(db_session)
    rival_profile = _rival_profile(db_session, season_id, LEAGUE_RIVAL_B_FPL_MANAGER_ID)
    # No raw_picks_json at all -- a genuinely manual squad with no picks recorded.
    snapshot = _create_manual_user_snapshot(db_session, season_id, raw_picks_json=None)

    job = _run_h2h_simulation(db_session, snapshot.id, rival_profile.id)

    assert job.status == JobStatus.SUCCEEDED
    run_id = uuid.UUID(job.output_json["h2h_simulation_run_id"])
    run_row = db_session.get(H2HSimulationRun, run_id)
    assert run_row is not None
    assert run_row.run_status == H2HSimulationRunStatus.SUCCEEDED
    total_probability = run_row.win_probability + run_row.draw_probability + run_row.loss_probability
    assert total_probability == pytest.approx(1, abs=1e-6)


def test_h2h_simulation_snapshot_with_unsolvable_squad_still_fails_cleanly(
    db_session, patched_fpl_client_optimization_with_active_gameweek, patched_fpl_client_league
):
    """The solver fallback degrades to the same clean failure as before when there's genuinely
    nothing to solve -- an empty squad can never produce a feasible starting XI."""
    season_id = _seed_players_with_active_gameweek(db_session)
    _run_league_sync(db_session)
    rival_profile = _rival_profile(db_session, season_id, LEAGUE_RIVAL_B_FPL_MANAGER_ID)
    snapshot = _create_manual_user_snapshot(db_session, season_id, raw_picks_json=None)
    snapshot.squad_player_ids = []
    db_session.commit()

    job = _run_h2h_simulation(db_session, snapshot.id, rival_profile.id)

    assert job.status == JobStatus.FAILED
    assert job.error_code == "H2H_SIMULATION_FAILED"


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
