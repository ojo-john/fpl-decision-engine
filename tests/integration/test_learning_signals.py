"""Integration tests for app.learning.signals against a real DB.

Mirrors ``tests/integration/test_failure_attribution.py``'s own approach --
every row ``compute_rolling_*``/``compute_failure_reason_concentration``
reads (``RecommendationAudit``/``RecommendationOutcome``/``OptimizationRun``/
``OptimizationRunPlayer``/``ModelVersion``/``ModelMetric``/``H2HSimulationRun``/
``FailureAttribution``) is built directly via the ORM, with real
``Player``/``Season``/``Gameweek``/``RulesConfig`` rows seeded via a real
``DATA_INGESTION`` run (``patched_fpl_client_optimization``) so every FK is
genuine.

Hand-picked ``player_gameweek_stats.total_points``/``h2h_simulation_runs
.win_probability`` values below are chosen so each per-gameweek MAE/Brier
score lands cleanly on one side of that signal's own degradation threshold
(see ``app.learning.signals``'s own named constants) -- never a near-tie.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from app.jobs.service import execute_job
from app.learning.attribution import FailureAttributionData, persist_failure_attributions
from app.learning.signals import (
    compute_failure_reason_concentration,
    compute_rolling_chip_recommendation_value,
    compute_rolling_h2h_calibration,
    compute_rolling_projection_mae,
)
from packages.database.models.counterfactual_result import CounterfactualResult
from packages.database.models.gameweek import Gameweek
from packages.database.models.h2h_simulation_run import H2HSimulationRun
from packages.database.models.manager_squad_snapshot import ManagerSquadSnapshot
from packages.database.models.model_metric import ModelMetric
from packages.database.models.model_version import ModelVersion
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
from packages.domain.jobs import JobStatus, JobType, TriggerType
from packages.domain.learning import (
    CounterfactualComparisonType,
    FailureCategory,
    FailureReasonCode,
    RecommendationConfidence,
)
from packages.domain.modeling import ModelFamily
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


def _gameweek(db_session, season_id, fpl_event_id: int) -> Gameweek:
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


def _create_champion_model_version(db_session, season_label: str, baseline_mae: float) -> uuid.UUID:
    # NOTE: deliberately does NOT set is_champion=True. compute_rolling_projection_mae resolves the
    # baseline walk-forward MAE via recommendation_audits.champion_model_version_id (a direct FK,
    # snapshotted at generation time), never via ModelVersion.is_champion -- so this test fixture has
    # no need to touch that column. Setting is_champion=True here would be actively harmful: it has
    # no DB-level uniqueness constraint (only app.modeling.champion.set_exactly_one_champion's
    # application-level discipline maintains "at most one True" across the whole table), and this
    # shared, non-truncated-between-tests Postgres is used by every other integration test in the
    # suite -- a stray extra is_champion=True row here would break
    # app.optimization.champion_loader.load_current_champion's own `scalar_one_or_none()` query for
    # every OTHER test that runs afterward in the same session.
    model_version = ModelVersion(
        model_name="test-champion",
        model_family=ModelFamily.BASELINE_SEASON_AVERAGE,
        feature_version=1,
        target_variable="total_points",
        training_seasons_json=[season_label],
        trained_at=_now(),
    )
    db_session.add(model_version)
    db_session.commit()
    db_session.refresh(model_version)

    db_session.add(
        ModelMetric(
            model_version_id=model_version.id,
            fold_index=None,
            mae=Decimal(str(baseline_mae)),
            rmse=Decimal(str(baseline_mae)),
            mean_bias=Decimal("0.0000"),
            n_observations=100,
        )
    )
    db_session.commit()
    return model_version.id


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


def _add_projection(db_session, optimization_run_id, player_id, projected_points: float) -> None:
    db_session.add(
        OptimizationRunPlayer(
            optimization_run_id=optimization_run_id,
            player_id=player_id,
            is_starting=True,
            is_captain=True,
            is_vice_captain=False,
            projected_points=Decimal(str(projected_points)),
            projection_source="test",
            now_cost_snapshot=80,
        )
    )
    db_session.commit()


def _seed_actual_points(db_session, player_id, season_id, gameweek_id, total_points: int) -> None:
    db_session.add(
        PlayerGameweekStats(
            player_id=player_id,
            season_id=season_id,
            gameweek_id=gameweek_id,
            minutes=90,
            total_points=total_points,
            data_source="test",
        )
    )
    db_session.commit()


def _create_audit(
    db_session, season_id, gameweek, champion_model_version_id=None, **overrides
) -> RecommendationAudit:
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
        champion_model_version_id=champion_model_version_id,
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


def _build_projection_gameweek(
    db_session, season_id, rules_config_id, fpl_event_id, model_version_id, actual_points: int
) -> None:
    """One gameweek's worth of recommendation_audits/optimization_run_players/player_gameweek_stats,
    a single-player projection run whose own abs error is fully controlled by ``actual_points``
    (projected is always fixed at 5.0 -- see each test's own chosen ``actual_points``).
    """
    gameweek = _gameweek(db_session, season_id, fpl_event_id)
    opt_run = _create_optimization_run(db_session, season_id, rules_config_id, gameweek)
    player_id = _player_id(db_session, season_id, 30000)
    _add_projection(db_session, opt_run.id, player_id, projected_points=5.0)
    _seed_actual_points(db_session, player_id, season_id, gameweek.id, total_points=actual_points)
    audit = _create_audit(
        db_session,
        season_id,
        gameweek,
        champion_model_version_id=model_version_id,
        optimization_run_id=opt_run.id,
    )
    _create_outcome(db_session, audit.id)


# --- compute_rolling_projection_mae -----------------------------------------------------------------


def test_compute_rolling_projection_mae_sustained_degradation_breaches(
    db_session, patched_fpl_client_optimization
):
    season_id = _seed_players(db_session)
    season = db_session.get(Season, season_id)
    rules_config_id = _seed_rules_config_id(db_session, season_id)
    model_version_id = _create_champion_model_version(db_session, season.label, baseline_mae=2.0)

    # projected=5.0 always; healthy actual=6.0 (abs_error=1.0, well under 2.0*1.25=2.5), degraded
    # actual=11.0 (abs_error=6.0, well over 2.5). Last 3 of 4 gameweeks are all degraded.
    _build_projection_gameweek(db_session, season_id, rules_config_id, 20, model_version_id, 6)
    _build_projection_gameweek(db_session, season_id, rules_config_id, 21, model_version_id, 11)
    _build_projection_gameweek(db_session, season_id, rules_config_id, 22, model_version_id, 11)
    _build_projection_gameweek(db_session, season_id, rules_config_id, 23, model_version_id, 11)

    as_of = _gameweek(db_session, season_id, 23)
    signal = compute_rolling_projection_mae(db_session, season_id, as_of.id, window_size_gameweeks=4)

    assert signal.sample_count == 4
    assert signal.window_size_gameweeks == 4
    assert signal.threshold_breached is True
    assert signal.threshold_config_snapshot["baseline_mae"] == 2.0


def test_compute_rolling_projection_mae_single_gameweek_blip_does_not_breach(
    db_session, patched_fpl_client_optimization
):
    season_id = _seed_players(db_session)
    season = db_session.get(Season, season_id)
    rules_config_id = _seed_rules_config_id(db_session, season_id)
    model_version_id = _create_champion_model_version(db_session, season.label, baseline_mae=2.0)

    # 3 healthy gameweeks, then ONE degraded blip on the most recent -- must NOT breach (only the
    # last 1 of the last 3 required is degraded, not all 3).
    _build_projection_gameweek(db_session, season_id, rules_config_id, 20, model_version_id, 6)
    _build_projection_gameweek(db_session, season_id, rules_config_id, 21, model_version_id, 6)
    _build_projection_gameweek(db_session, season_id, rules_config_id, 22, model_version_id, 6)
    _build_projection_gameweek(db_session, season_id, rules_config_id, 23, model_version_id, 11)

    as_of = _gameweek(db_session, season_id, 23)
    signal = compute_rolling_projection_mae(db_session, season_id, as_of.id, window_size_gameweeks=4)

    assert signal.sample_count == 4
    assert signal.threshold_breached is False


def test_compute_rolling_projection_mae_no_history_returns_zero_sample_count(
    db_session, patched_fpl_client_optimization
):
    season_id = _seed_players(db_session)
    as_of = _gameweek(db_session, season_id, 20)

    signal = compute_rolling_projection_mae(db_session, season_id, as_of.id)

    assert signal.sample_count == 0
    assert signal.threshold_breached is False
    assert signal.window_size_gameweeks == 0


# --- compute_rolling_h2h_calibration -----------------------------------------------------------------


def _build_h2h_gameweek(
    db_session, season_id, fpl_event_id, manager_squad_snapshot_id, rival_profile_id, win_probability, result
) -> None:
    gameweek = _gameweek(db_session, season_id, fpl_event_id)
    h2h_run = H2HSimulationRun(
        manager_squad_snapshot_id=manager_squad_snapshot_id,
        rival_profile_id=rival_profile_id,
        run_status=H2HSimulationRunStatus.SUCCEEDED,
        target_fpl_event_id=gameweek.fpl_event_id,
        simulation_method_version=1,
        win_probability=Decimal(str(win_probability)),
        rules_status_snapshot=RulesStatus.OK,
        optimization_permitted_snapshot=True,
        started_at=_now(),
        completed_at=_now(),
    )
    db_session.add(h2h_run)
    db_session.commit()
    db_session.refresh(h2h_run)

    audit = _create_audit(db_session, season_id, gameweek, h2h_simulation_run_id=h2h_run.id)
    _create_outcome(db_session, audit.id, h2h_result=result)


def _seed_h2h_context(db_session, season_id) -> tuple[uuid.UUID, uuid.UUID]:
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
    rival_profile = RivalProfile(
        season_id=season_id,
        fpl_manager_id=900002,
        overall_confidence=RivalStateConfidence.MEDIUM,
        computed_at=_now(),
    )
    db_session.add(rival_profile)
    db_session.commit()
    db_session.refresh(snapshot)
    db_session.refresh(rival_profile)
    return snapshot.id, rival_profile.id


def test_compute_rolling_h2h_calibration_sustained_miscalibration_breaches(
    db_session, patched_fpl_client_optimization
):
    season_id = _seed_players(db_session)
    snapshot_id, rival_profile_id = _seed_h2h_context(db_session, season_id)

    # Healthy: predicted 0.6, won -> brier=0.16 (< 0.30). Degraded: predicted 0.9, lost -> brier=0.81.
    _build_h2h_gameweek(db_session, season_id, 20, snapshot_id, rival_profile_id, 0.6, "win")
    _build_h2h_gameweek(db_session, season_id, 21, snapshot_id, rival_profile_id, 0.9, "loss")
    _build_h2h_gameweek(db_session, season_id, 22, snapshot_id, rival_profile_id, 0.9, "loss")
    _build_h2h_gameweek(db_session, season_id, 23, snapshot_id, rival_profile_id, 0.9, "loss")

    as_of = _gameweek(db_session, season_id, 23)
    signal = compute_rolling_h2h_calibration(db_session, season_id, as_of.id, window_size_gameweeks=4)

    assert signal.sample_count == 4
    assert signal.threshold_breached is True


def test_compute_rolling_h2h_calibration_single_match_blip_does_not_breach(
    db_session, patched_fpl_client_optimization
):
    season_id = _seed_players(db_session)
    snapshot_id, rival_profile_id = _seed_h2h_context(db_session, season_id)

    _build_h2h_gameweek(db_session, season_id, 20, snapshot_id, rival_profile_id, 0.6, "win")
    _build_h2h_gameweek(db_session, season_id, 21, snapshot_id, rival_profile_id, 0.6, "win")
    _build_h2h_gameweek(db_session, season_id, 22, snapshot_id, rival_profile_id, 0.6, "win")
    _build_h2h_gameweek(db_session, season_id, 23, snapshot_id, rival_profile_id, 0.9, "loss")

    as_of = _gameweek(db_session, season_id, 23)
    signal = compute_rolling_h2h_calibration(db_session, season_id, as_of.id, window_size_gameweeks=4)

    assert signal.sample_count == 4
    assert signal.threshold_breached is False


# --- compute_rolling_chip_recommendation_value -------------------------------------------------------


def test_compute_rolling_chip_recommendation_value_is_informational_only(
    db_session, patched_fpl_client_optimization
):
    season_id = _seed_players(db_session)

    for fpl_event_id, delta in ((20, 4.0), (21, -2.0), (22, 6.0)):
        gameweek = _gameweek(db_session, season_id, fpl_event_id)
        audit = _create_audit(db_session, season_id, gameweek)
        outcome = _create_outcome(db_session, audit.id)
        db_session.add(
            CounterfactualResult(
                recommendation_outcome_id=outcome.id,
                comparison_type=CounterfactualComparisonType.CHIP_VS_HOLD,
                baseline_points=Decimal("0.00"),
                alternative_points=Decimal(str(delta)),
                delta=Decimal(str(delta)),
                is_value_adding=delta > 0,
                baseline_description_json={},
                alternative_description_json={},
            )
        )
        db_session.commit()

    as_of = _gameweek(db_session, season_id, 22)
    signal = compute_rolling_chip_recommendation_value(db_session, season_id, as_of.id)

    assert signal.sample_count == 3
    assert signal.value == Decimal(str(round((4.0 - 2.0 + 6.0) / 3, 4)))
    # Informational only -- never a retraining-decision signal, always False.
    assert signal.threshold_breached is False
    assert signal.threshold_config_snapshot["informational_only"] is True
    assert signal.window_size_gameweeks == 22


# --- compute_failure_reason_concentration ------------------------------------------------------------


def _seed_failure_batch(db_session, season_id, fpl_event_id, reason_codes: list[FailureReasonCode]) -> None:
    gameweek = _gameweek(db_session, season_id, fpl_event_id)
    audit = _create_audit(db_session, season_id, gameweek)
    outcome = _create_outcome(db_session, audit.id)
    attributions = [
        FailureAttributionData(
            reason_code=code, category=FailureCategory.OPTIMIZATION, severity=None, evidence={}
        )
        for code in reason_codes
    ]
    persist_failure_attributions(db_session, outcome.id, attributions)


def test_compute_failure_reason_concentration_repeated_pattern_breaches(
    db_session, patched_fpl_client_optimization
):
    season_id = _seed_players(db_session)

    _seed_failure_batch(db_session, season_id, 20, [FailureReasonCode.CHIP_TIMING_SUBOPTIMAL])
    _seed_failure_batch(db_session, season_id, 21, [FailureReasonCode.CHIP_TIMING_SUBOPTIMAL])
    _seed_failure_batch(db_session, season_id, 22, [FailureReasonCode.CHIP_TIMING_SUBOPTIMAL])
    _seed_failure_batch(db_session, season_id, 23, [FailureReasonCode.VARIANCE_UNEXPLAINED])

    as_of = _gameweek(db_session, season_id, 23)
    signal = compute_failure_reason_concentration(db_session, season_id, as_of.id, lookback_batches=4)

    assert signal.sample_count == 4
    assert signal.threshold_breached is True
    assert signal.threshold_config_snapshot["most_concentrated_reason_code"] == "chip_timing_suboptimal"


def test_compute_failure_reason_concentration_varied_pattern_does_not_breach(
    db_session, patched_fpl_client_optimization
):
    season_id = _seed_players(db_session)

    _seed_failure_batch(db_session, season_id, 20, [FailureReasonCode.CHIP_TIMING_SUBOPTIMAL])
    _seed_failure_batch(db_session, season_id, 21, [FailureReasonCode.PROJECTION_OVERESTIMATED])
    _seed_failure_batch(db_session, season_id, 22, [FailureReasonCode.RIVAL_PREDICTION_ERROR])
    _seed_failure_batch(db_session, season_id, 23, [FailureReasonCode.TRANSFER_DECISION_SUBOPTIMAL])

    as_of = _gameweek(db_session, season_id, 23)
    signal = compute_failure_reason_concentration(db_session, season_id, as_of.id, lookback_batches=4)

    assert signal.sample_count == 4
    assert signal.threshold_breached is False
