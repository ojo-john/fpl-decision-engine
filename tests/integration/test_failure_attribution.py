"""Integration tests for app.learning.attribution against a real DB.

Mirrors ``tests/integration/test_outcome_ingestion.py``'s own approach:
``RecommendationOutcome``/``CounterfactualResult`` rows (and the surrounding
``RecommendationAudit``/``OptimizationRun``/``OptimizationRunPlayer``/
``H2HSimulationRun``/``RivalProfile`` context ``classify_failure_attributions``
reads) are built directly via the ORM rather than driving the real job
handlers end to end -- ``classify_failure_attributions`` only ever reads
already-persisted rows by FK, so it cannot tell the difference, and
hand-building is the only practical way to deterministically exercise each
``FailureReasonCode``'s own detection rule in isolation. Real
``Player``/``Season``/``Gameweek``/``RulesConfig`` rows are still seeded via a
real ``DATA_INGESTION`` run (``patched_fpl_client_optimization``) so every FK
here is a genuine row in a genuine Postgres.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from app.jobs.service import execute_job
from app.learning.attribution import (
    attribute_failures_for_outcome,
    classify_failure_attributions,
    persist_failure_attributions,
)
from packages.database.models.counterfactual_result import CounterfactualResult
from packages.database.models.failure_attribution import FailureAttribution
from packages.database.models.gameweek import Gameweek
from packages.database.models.h2h_simulation_run import H2HSimulationRun
from packages.database.models.manager_squad_snapshot import ManagerSquadSnapshot
from packages.database.models.optimization_run import OptimizationRun
from packages.database.models.optimization_run_player import OptimizationRunPlayer
from packages.database.models.player import Player
from packages.database.models.player_gameweek_stats import PlayerGameweekStats
from packages.database.models.recommendation_audit import RecommendationAudit
from packages.database.models.recommendation_outcome import RecommendationOutcome
from packages.database.models.rival_profile import RivalProfile
from packages.database.models.rules_config import RulesConfig
from packages.database.models.season import Season
from packages.domain.h2h import H2HSimulationRunStatus, RivalStateConfidence
from packages.domain.ingestion import EndpointHealth
from packages.domain.jobs import JobStatus, JobType, TriggerType
from packages.domain.learning import (
    CounterfactualComparisonType,
    FailureCategory,
    FailureReasonCode,
    RecommendationConfidence,
)
from packages.domain.optimization import (
    OptimizationRunStatus,
    RecommendationType,
    SolverStatus,
    TargetGameweekStatus,
)
from packages.domain.rules import RulesStatus
from packages.domain.transfers import ManagerSquadSnapshotSource


def _now():
    return datetime.now(UTC)


def _seed_players(db_session) -> uuid.UUID:
    ingestion_job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert ingestion_job.status == JobStatus.SUCCEEDED
    return db_session.execute(
        select(Season.id).where(Season.label == ingestion_job.output_json["season_label"])
    ).scalar_one()


def _target_gameweek(db_session, season_id, fpl_event_id: int = 20) -> Gameweek:
    return db_session.execute(
        select(Gameweek).where(Gameweek.season_id == season_id, Gameweek.fpl_event_id == fpl_event_id)
    ).scalar_one()


def _player_id(db_session, season_id, element_id: int) -> uuid.UUID:
    return db_session.execute(
        select(Player.id).where(Player.season_id == season_id, Player.fpl_element_id == element_id)
    ).scalar_one()


def _seed_rules_config_id(db_session, season_id) -> uuid.UUID:
    return db_session.execute(
        select(RulesConfig.id).where(RulesConfig.season_id == season_id, RulesConfig.is_current.is_(True))
    ).scalar_one()


def _create_audit(db_session, season_id, gameweek, **overrides) -> RecommendationAudit:
    defaults: dict = dict(
        season_id=season_id,
        target_gameweek_id=gameweek.id,
        target_fpl_event_id=gameweek.fpl_event_id,
        is_authoritative=True,
        confidence=RecommendationConfidence.HIGH,
        confidence_drivers_json=[],
        assumptions_json=[],
        known_risks_json=[],
        generated_at=_now(),
    )
    defaults.update(overrides)
    audit = RecommendationAudit(**defaults)
    db_session.add(audit)
    db_session.commit()
    db_session.refresh(audit)
    return audit


def _create_outcome(db_session, audit_id, **overrides) -> RecommendationOutcome:
    defaults: dict = dict(recommendation_audit_id=audit_id, finalized_at=_now())
    defaults.update(overrides)
    outcome = RecommendationOutcome(**defaults)
    db_session.add(outcome)
    db_session.commit()
    db_session.refresh(outcome)
    return outcome


def _create_counterfactual_result(
    db_session, outcome_id, comparison_type, baseline: float, alternative: float
) -> CounterfactualResult:
    delta = alternative - baseline
    row = CounterfactualResult(
        recommendation_outcome_id=outcome_id,
        comparison_type=comparison_type,
        baseline_points=Decimal(str(baseline)),
        alternative_points=Decimal(str(alternative)),
        delta=Decimal(str(delta)),
        is_value_adding=delta > 0,
        baseline_description_json={},
        alternative_description_json={},
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _create_optimization_run(db_session, season_id, rules_config_id, gameweek) -> OptimizationRun:
    run = OptimizationRun(
        season_id=season_id,
        rules_config_id=rules_config_id,
        recommendation_type=RecommendationType.JOINT_SQUAD_AND_XI,
        run_status=OptimizationRunStatus.SUCCEEDED,
        solver_status=SolverStatus.OPTIMAL,
        target_gameweek_id=gameweek.id,
        target_gameweek_status=TargetGameweekStatus.RESOLVED,
        optimization_permitted_snapshot=True,
        data_quality_score_snapshot=Decimal("100.00"),
        rules_status_snapshot=RulesStatus.OK,
        projection_formula_version=1,
        started_at=_now(),
        completed_at=_now(),
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    return run


def _add_captain_projection(
    db_session, optimization_run_id, player_id, projected_points: float
) -> OptimizationRunPlayer:
    row = OptimizationRunPlayer(
        optimization_run_id=optimization_run_id,
        player_id=player_id,
        is_starting=True,
        is_captain=True,
        is_vice_captain=False,
        projected_points=Decimal(str(projected_points)),
        projection_source="test",
        now_cost_snapshot=80,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _seed_actual_points(db_session, player_id, season_id, gameweek_id, minutes: int, total_points: int):
    db_session.add(
        PlayerGameweekStats(
            player_id=player_id,
            season_id=season_id,
            gameweek_id=gameweek_id,
            minutes=minutes,
            total_points=total_points,
            data_source="test",
        )
    )
    db_session.commit()


def _create_manager_squad_snapshot(db_session, season_id) -> ManagerSquadSnapshot:
    snapshot = ManagerSquadSnapshot(
        season_id=season_id,
        source=ManagerSquadSnapshotSource.MANUAL_ENTRY,
        bank=0,
        free_transfers_available=1,
        squad_player_ids=[],
        squad_player_purchase_prices_json={},
        squad_player_selling_prices_json={},
    )
    db_session.add(snapshot)
    db_session.commit()
    db_session.refresh(snapshot)
    return snapshot


def _create_h2h_run(db_session, gameweek, manager_squad_snapshot_id, rival_profile_id) -> H2HSimulationRun:
    run = H2HSimulationRun(
        manager_squad_snapshot_id=manager_squad_snapshot_id,
        rival_profile_id=rival_profile_id,
        run_status=H2HSimulationRunStatus.SUCCEEDED,
        target_fpl_event_id=gameweek.fpl_event_id,
        simulation_method_version=1,
        rules_status_snapshot=RulesStatus.OK,
        optimization_permitted_snapshot=True,
        started_at=_now(),
        completed_at=_now(),
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    return run


def _create_rival_profile(db_session, season_id, predicted_captain_probabilities: dict) -> RivalProfile:
    profile = RivalProfile(
        season_id=season_id,
        fpl_manager_id=900001,
        overall_confidence=RivalStateConfidence.MEDIUM,
        predicted_captain_probabilities_json=predicted_captain_probabilities,
        computed_at=_now(),
    )
    db_session.add(profile)
    db_session.commit()
    db_session.refresh(profile)
    return profile


# --- FPL_API_DEGRADED_OR_BLOCKED + CHIP_TIMING_SUBOPTIMAL ------------------------------------------


def test_classify_degraded_api_and_material_chip_miss_attaches_both_with_evidence(
    db_session, patched_fpl_client_optimization
):
    season_id = _seed_players(db_session)
    gameweek = _target_gameweek(db_session, season_id)
    audit = _create_audit(
        db_session, season_id, gameweek, fpl_api_health_at_generation=EndpointHealth.DEGRADED
    )
    outcome = _create_outcome(db_session, audit.id)
    chip_result = _create_counterfactual_result(
        db_session,
        outcome.id,
        CounterfactualComparisonType.CHIP_VS_HOLD,
        baseline=0.0,
        alternative=-8.5,
    )

    attributions = classify_failure_attributions(db_session, outcome, [chip_result], audit)

    codes = {a.reason_code for a in attributions}
    assert codes == {FailureReasonCode.FPL_API_DEGRADED_OR_BLOCKED, FailureReasonCode.CHIP_TIMING_SUBOPTIMAL}

    api_attribution = next(
        a for a in attributions if a.reason_code == FailureReasonCode.FPL_API_DEGRADED_OR_BLOCKED
    )
    assert api_attribution.category == FailureCategory.DATA
    assert api_attribution.evidence == {"fpl_api_health_at_generation": "degraded"}

    chip_attribution = next(
        a for a in attributions if a.reason_code == FailureReasonCode.CHIP_TIMING_SUBOPTIMAL
    )
    assert chip_attribution.category == FailureCategory.OPTIMIZATION
    assert chip_attribution.severity == Decimal("8.50")
    assert chip_attribution.evidence == {
        "counterfactual_comparison_type": "chip_vs_hold",
        "delta": -8.5,
    }

    persisted = persist_failure_attributions(db_session, outcome.id, attributions)
    assert len(persisted) == 2
    rows = (
        db_session.execute(
            select(FailureAttribution).where(FailureAttribution.recommendation_outcome_id == outcome.id)
        )
        .scalars()
        .all()
    )
    assert {row.reason_code for row in rows} == codes


# --- RECOMMENDATION_WAS_SOUND ------------------------------------------------------------------------


def test_classify_clean_sound_gameweek_attaches_exactly_one_recommendation_was_sound(
    db_session, patched_fpl_client_optimization
):
    season_id = _seed_players(db_session)
    gameweek = _target_gameweek(db_session, season_id)
    audit = _create_audit(
        db_session, season_id, gameweek, fpl_api_health_at_generation=EndpointHealth.HEALTHY
    )
    outcome = _create_outcome(db_session, audit.id)

    attributions = classify_failure_attributions(db_session, outcome, [], audit)

    assert len(attributions) == 1
    assert attributions[0].reason_code == FailureReasonCode.RECOMMENDATION_WAS_SOUND
    assert attributions[0].category == FailureCategory.VARIANCE


def test_classify_small_below_threshold_deltas_do_not_get_over_attributed(
    db_session, patched_fpl_client_optimization
):
    # Small, real-world-irrelevant deltas in EITHER direction must not generate an attribution --
    # only RECOMMENDATION_WAS_SOUND should be returned.
    season_id = _seed_players(db_session)
    gameweek = _target_gameweek(db_session, season_id)
    audit = _create_audit(db_session, season_id, gameweek)
    outcome = _create_outcome(db_session, audit.id)
    team_result = _create_counterfactual_result(
        db_session, outcome.id, CounterfactualComparisonType.RECOMMENDED_VS_ACTUAL_TEAM, 60.0, 62.0
    )
    chip_result = _create_counterfactual_result(
        db_session, outcome.id, CounterfactualComparisonType.CHIP_VS_HOLD, 0.0, -1.5
    )

    attributions = classify_failure_attributions(db_session, outcome, [team_result, chip_result], audit)

    assert len(attributions) == 1
    assert attributions[0].reason_code == FailureReasonCode.RECOMMENDATION_WAS_SOUND


# --- Captain projection accuracy / rotation --------------------------------------------------------


def test_classify_captain_zero_minutes_with_material_projection_is_player_rotated(
    db_session, patched_fpl_client_optimization
):
    season_id = _seed_players(db_session)
    gameweek = _target_gameweek(db_session, season_id)
    rules_config_id = _seed_rules_config_id(db_session, season_id)
    opt_run = _create_optimization_run(db_session, season_id, rules_config_id, gameweek)
    captain_player_id = _player_id(db_session, season_id, 30000)
    _add_captain_projection(db_session, opt_run.id, captain_player_id, projected_points=7.0)
    # No PlayerGameweekStats row at all for this player/gameweek -- never featured.

    audit = _create_audit(db_session, season_id, gameweek, optimization_run_id=opt_run.id)
    outcome = _create_outcome(db_session, audit.id)

    attributions = classify_failure_attributions(db_session, outcome, [], audit)

    rotated = next(
        a for a in attributions if a.reason_code == FailureReasonCode.PLAYER_ROTATED_OR_UNAVAILABLE
    )
    assert rotated.category == FailureCategory.MINUTES_AVAILABILITY
    assert rotated.evidence["player_id"] == str(captain_player_id)
    assert rotated.evidence["actual_minutes"] == 0
    assert FailureReasonCode.RECOMMENDATION_WAS_SOUND not in {a.reason_code for a in attributions}


def test_classify_captain_played_but_overestimated(db_session, patched_fpl_client_optimization):
    season_id = _seed_players(db_session)
    gameweek = _target_gameweek(db_session, season_id)
    rules_config_id = _seed_rules_config_id(db_session, season_id)
    opt_run = _create_optimization_run(db_session, season_id, rules_config_id, gameweek)
    captain_player_id = _player_id(db_session, season_id, 30000)
    _add_captain_projection(db_session, opt_run.id, captain_player_id, projected_points=9.0)
    _seed_actual_points(db_session, captain_player_id, season_id, gameweek.id, minutes=90, total_points=1)

    audit = _create_audit(db_session, season_id, gameweek, optimization_run_id=opt_run.id)
    outcome = _create_outcome(db_session, audit.id)

    attributions = classify_failure_attributions(db_session, outcome, [], audit)

    overestimated = next(
        a for a in attributions if a.reason_code == FailureReasonCode.PROJECTION_OVERESTIMATED
    )
    assert overestimated.category == FailureCategory.PERFORMANCE_MODEL
    assert overestimated.evidence["projected_points"] == 9.0
    assert overestimated.evidence["actual_raw_points"] == 1.0


def test_classify_captain_played_but_underestimated(db_session, patched_fpl_client_optimization):
    season_id = _seed_players(db_session)
    gameweek = _target_gameweek(db_session, season_id)
    rules_config_id = _seed_rules_config_id(db_session, season_id)
    opt_run = _create_optimization_run(db_session, season_id, rules_config_id, gameweek)
    captain_player_id = _player_id(db_session, season_id, 30000)
    _add_captain_projection(db_session, opt_run.id, captain_player_id, projected_points=2.0)
    _seed_actual_points(db_session, captain_player_id, season_id, gameweek.id, minutes=90, total_points=15)

    audit = _create_audit(db_session, season_id, gameweek, optimization_run_id=opt_run.id)
    outcome = _create_outcome(db_session, audit.id)

    attributions = classify_failure_attributions(db_session, outcome, [], audit)

    underestimated = next(
        a for a in attributions if a.reason_code == FailureReasonCode.PROJECTION_UNDERESTIMATED
    )
    assert underestimated.category == FailureCategory.PERFORMANCE_MODEL
    assert underestimated.evidence["actual_raw_points"] == 15.0


# --- RIVAL_PREDICTION_ERROR -------------------------------------------------------------------------


def test_classify_rival_prediction_error_confident_and_wrong(db_session, patched_fpl_client_optimization):
    season_id = _seed_players(db_session)
    gameweek = _target_gameweek(db_session, season_id)
    manager_squad_snapshot = _create_manager_squad_snapshot(db_session, season_id)
    rival_profile = _create_rival_profile(
        db_session, season_id, predicted_captain_probabilities={"111": 0.7, "222": 0.3}
    )
    h2h_run = _create_h2h_run(db_session, gameweek, manager_squad_snapshot.id, rival_profile.id)

    audit = _create_audit(db_session, season_id, gameweek, h2h_simulation_run_id=h2h_run.id)
    outcome = _create_outcome(db_session, audit.id, opponent_actual_captain_player_id=222)

    attributions = classify_failure_attributions(db_session, outcome, [], audit)

    rival_error = next(a for a in attributions if a.reason_code == FailureReasonCode.RIVAL_PREDICTION_ERROR)
    assert rival_error.category == FailureCategory.H2H
    assert rival_error.evidence == {
        "predicted_captain_element_id": 111,
        "predicted_probability": 0.7,
        "actual_captain_element_id": 222,
    }


def test_classify_rival_prediction_correct_is_not_attributed(db_session, patched_fpl_client_optimization):
    season_id = _seed_players(db_session)
    gameweek = _target_gameweek(db_session, season_id)
    manager_squad_snapshot = _create_manager_squad_snapshot(db_session, season_id)
    rival_profile = _create_rival_profile(
        db_session, season_id, predicted_captain_probabilities={"111": 0.7, "222": 0.3}
    )
    h2h_run = _create_h2h_run(db_session, gameweek, manager_squad_snapshot.id, rival_profile.id)

    audit = _create_audit(db_session, season_id, gameweek, h2h_simulation_run_id=h2h_run.id)
    outcome = _create_outcome(db_session, audit.id, opponent_actual_captain_player_id=111)

    attributions = classify_failure_attributions(db_session, outcome, [], audit)

    assert FailureReasonCode.RIVAL_PREDICTION_ERROR not in {a.reason_code for a in attributions}
    assert attributions[0].reason_code == FailureReasonCode.RECOMMENDATION_WAS_SOUND


# --- attribute_failures_for_outcome (end-to-end persistence + idempotent rerun) --------------------


def test_attribute_failures_for_outcome_persists_and_reruns_idempotently(
    db_session, patched_fpl_client_optimization
):
    season_id = _seed_players(db_session)
    gameweek = _target_gameweek(db_session, season_id)
    audit = _create_audit(
        db_session, season_id, gameweek, fpl_api_health_at_generation=EndpointHealth.BLOCKED
    )
    outcome = _create_outcome(db_session, audit.id)

    first_rows = attribute_failures_for_outcome(db_session, outcome.id)
    assert len(first_rows) == 1
    assert first_rows[0].reason_code == FailureReasonCode.FPL_API_DEGRADED_OR_BLOCKED

    second_rows = attribute_failures_for_outcome(db_session, outcome.id)
    assert len(second_rows) == 1

    all_rows = (
        db_session.execute(
            select(FailureAttribution).where(FailureAttribution.recommendation_outcome_id == outcome.id)
        )
        .scalars()
        .all()
    )
    assert len(all_rows) == 1
