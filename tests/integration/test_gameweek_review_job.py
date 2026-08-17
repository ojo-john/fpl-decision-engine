"""Integration tests for app.jobs.handlers.gameweek_review.GameweekReviewJobRunner against a real DB.

Mirrors ``tests/integration/test_learning_signals.py``'s own approach: real
``Player``/``Season``/``Gameweek``/``RulesConfig`` rows are seeded via a real
``DATA_INGESTION`` run (``patched_fpl_client_optimization``), and every other
row this job's own building blocks (Layer 2's ``ingest_outcome_for_audit``,
Layer 3's ``attribute_failures_for_outcome``/``compute_rolling_*``) read is
hand-built directly via the ORM -- those functions are already independently,
thoroughly covered by their own test modules
(``test_outcome_ingestion.py``/``test_failure_attribution.py``/
``test_learning_signals.py``); this module's own job is to prove they are
correctly SEQUENCED and their outputs correctly COMPOSED into one
``learning_reports`` row, not to re-prove their own internal correctness.

The happy-path scenario is deliberately built to exercise a REAL, material
finding, not just an all-sound review: the reviewed gameweek's own
recommended captain is given a wildly overestimated projection (15.0
projected vs. 2.0 real raw points, well past
``app.learning.attribution._MATERIAL_PROJECTION_ERROR_POINTS``), which
mechanically produces one ``PROJECTION_OVERESTIMATED`` failure attribution.
Two earlier gameweeks are seeded with the same degraded pattern (a fixed
champion baseline MAE of 2.0, so 3 consecutive abs-errors of 13.0 sustain a
breach) so ``ROLLING_PROJECTION_MAE`` breaches too, and those same two
earlier gameweeks' outcomes are seeded with the SAME
``PROJECTION_OVERESTIMATED`` reason code (directly, via
``persist_failure_attributions`` -- bypassing a full ``ingest_outcome_for_audit``
call for gameweeks this job itself never reviews) so
``FAILURE_REASON_CONCENTRATION`` breaches too. This gives the happy-path test
a real, hand-verified ``notable_findings_json`` to assert against, not an
empty list.

Milestone 7 Layer 6 (BACKTEST job + the GAMEWEEK_REVIEW -> MODEL_TRAINING
retraining-trigger wiring) adds its own dedicated tests near the bottom of
this module. One side effect worth calling out explicitly: the happy-path
scenario above's own sustained ``ROLLING_PROJECTION_MAE`` breach now ALSO
triggers a real retraining attempt as a side effect (that is the new, correct
behavior this layer adds, not a regression) -- the happy-path test's own
``notable_finding_count``/``notable_findings_json`` assertions account for
this. ``_clear_model_lifecycle_state_around_test`` (autouse, this whole
module) resets ``model_versions.is_champion``/``lifecycle_state`` before AND
after every test here, exactly like ``test_promotion.py``'s own precedent --
required because a real, nested ``MODEL_TRAINING`` run's own
bootstrap-vs-shadow-challenger decision (and this module's own Layer 6 tests'
determinism) depends on that global state, on this shared, non-truncated
Postgres.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select, update

from app.jobs.service import execute_job
from app.learning.attribution import FailureAttributionData, persist_failure_attributions
from app.optimization.projection import AVAILABILITY_RISK_NOTED_SUFFIX
from app.players.price_signal_calibration import MIN_CALIBRATION_SAMPLE_SIZE
from packages.database.models.chip_optimization_run import ChipOptimizationRun  # noqa: F401 (registers model)
from packages.database.models.failure_attribution import FailureAttribution
from packages.database.models.gameweek import Gameweek
from packages.database.models.job import Job
from packages.database.models.learning_report import LearningReport
from packages.database.models.learning_signal import LearningSignal
from packages.database.models.model_metric import ModelMetric
from packages.database.models.model_promotion import ModelPromotion
from packages.database.models.model_version import ModelVersion
from packages.database.models.note_effectiveness_evaluation import NoteEffectivenessEvaluation
from packages.database.models.optimization_run import OptimizationRun
from packages.database.models.optimization_run_player import OptimizationRunPlayer
from packages.database.models.player import Player
from packages.database.models.player_availability_risk_note import PlayerAvailabilityRiskNote
from packages.database.models.player_gameweek_stats import PlayerGameweekStats
from packages.database.models.player_price_history import PlayerPriceHistory
from packages.database.models.player_price_signal_observation import PlayerPriceSignalObservation
from packages.database.models.price_signal_calibration import PriceSignalCalibration
from packages.database.models.recommendation_audit import RecommendationAudit
from packages.database.models.recommendation_outcome import RecommendationOutcome
from packages.database.models.rules_config import RulesConfig
from packages.database.models.season import Season
from packages.domain.jobs import JobStatus, JobType, TriggerType
from packages.domain.learning import (
    FailureCategory,
    FailureReasonCode,
    GameweekReviewRunStatus,
    LearningSignalType,
    ModelLifecycleState,
    PromotionVerdict,
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

# Milestone 7 Layer 6: a deliberately implausible sentinel feature_version, guaranteed to never
# have any real player_features rows anywhere in this shared test database -- used by the
# retraining-trigger-fails scenario below to make a nested MODEL_TRAINING run's own
# NO_SEASONS_WITH_FEATURES failure fully deterministic, regardless of what other test modules may
# have already ingested/generated features for feature_version=1 earlier in the same pytest session.
_NO_FEATURES_SENTINEL_FEATURE_VERSION = 987654321

_CAPTAIN_ELEMENT_ID = 30000
_REVIEWED_FPL_EVENT_ID = 20
_PRIOR_FPL_EVENT_IDS = (18, 19)
_BASELINE_MAE = 2.0
# projected=15.0, actual raw=2.0 -> abs_error=13.0, well past both the projection-MAE degradation
# bar (2.0*1.25=2.5) and the captain projection-accuracy materiality bar (4.0).
_PROJECTED_POINTS = 15.0
_ACTUAL_RAW_POINTS = 2


def _now():
    return datetime.now(UTC)


def _clear_global_model_lifecycle_state(db_session) -> None:
    """Blanket reset across the WHOLE ``model_versions`` table -- mirrors
    ``tests/integration/test_promotion.py``'s own ``_clear_global_model_lifecycle_state`` (Milestone
    7 Layer 5's own precedent for this exact cross-test-pollution risk).

    Milestone 7 Layer 6's retraining-trigger wiring means this module's own "sustained MAE breach"
    scenario -- already exercised by the pre-existing happy-path/rerun tests below, which construct
    exactly such a breach to prove ``ROLLING_PROJECTION_MAE.threshold_breached=True`` -- now ALSO
    triggers a real, nested ``MODEL_TRAINING`` job as a side effect. Without this fixture, whether
    that nested run takes the bootstrap-champion or shadow-challenger path (and whether it succeeds
    or fails) would depend on whatever ``is_champion``/``lifecycle_state`` state happens to be left
    over from other test modules that already ran earlier in this same pytest session, since this
    whole suite shares one persistent, non-truncated Postgres (see ``conftest.py``'s own docstring).
    """
    db_session.execute(
        update(ModelVersion).values(is_champion=False, lifecycle_state=ModelLifecycleState.CANDIDATE)
    )
    db_session.commit()


@pytest.fixture(autouse=True)
def _clear_model_lifecycle_state_around_test(db_session):
    _clear_global_model_lifecycle_state(db_session)
    yield
    _clear_global_model_lifecycle_state(db_session)


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
    # Deliberately does NOT set is_champion=True -- see test_learning_signals.py's own
    # _create_champion_model_version docstring for why that would be actively harmful on this
    # shared, non-truncated-between-tests Postgres (a stray is_champion=True row breaks every OTHER
    # test's own app.optimization.champion_loader.load_current_champion query).
    model_version = ModelVersion(
        model_name="gw-review-test-champion",
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


def _create_global_champion_model_version(
    db_session, season_label: str, baseline_mae: float, feature_version: int = 1
) -> uuid.UUID:
    """Like ``_create_champion_model_version``, but ALSO flags this row the REAL global champion
    (``is_champion=True``, ``lifecycle_state=CHAMPION``) -- needed only by this module's own
    Layer 6 retraining-trigger tests, which need
    ``app.jobs.handlers.gameweek_review``'s own ``_resolve_feature_version_for_retraining``/
    ``_trigger_retraining_and_promotion_check`` to resolve a real ``(feature_version, champion)``
    pair the same way production does. Safe here -- unlike ``_create_champion_model_version``'s own
    deliberate avoidance of this -- ONLY because ``_clear_model_lifecycle_state_around_test`` above
    guarantees this is reset before AND after every test in this module, so it can never leak into
    another test module.
    """
    model_version = ModelVersion(
        model_name="gw-review-test-global-champion",
        model_family=ModelFamily.BASELINE_SEASON_AVERAGE,
        feature_version=feature_version,
        target_variable="total_points",
        training_seasons_json=[season_label],
        is_champion=True,
        lifecycle_state=ModelLifecycleState.CHAMPION,
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


def _seed_historical_season_for_model_training(db_session) -> None:
    """Real historical ``2023-24`` ``player_features`` rows (``feature_version=1``), so a nested
    ``MODEL_TRAINING`` run triggered mid-GAMEWEEK_REVIEW has genuine data to train against.

    Duplicated (not imported) from ``tests/integration/test_model_training_job.py``'s own
    ``_seed_historical_season`` -- mirrors this repo's own established "duplicate test setup rather
    than cross-import between test modules" convention (see e.g. ``test_api_learning.py``'s own
    module docstring). Idempotent to call again for the same ``season_label`` -- already relied on
    by ``test_model_training_job.py``'s own multiple, independent tests doing exactly this.
    """
    ingestion_job = execute_job(
        db_session,
        job_type=JobType.HISTORICAL_DATA_INGESTION,
        parameters={"season_label": "2023-24"},
        trigger_type=TriggerType.LOCAL,
    )
    assert ingestion_job.status == JobStatus.SUCCEEDED
    features_job = execute_job(
        db_session,
        job_type=JobType.FEATURE_GENERATION,
        parameters={"season_label": "2023-24"},
        trigger_type=TriggerType.LOCAL,
    )
    assert features_job.status == JobStatus.SUCCEEDED


def _create_optimization_run_with_captain_projection(
    db_session, season_id, rules_config_id, gameweek, captain_player_id, projected_points: float
) -> OptimizationRun:
    run = OptimizationRun(
        season_id=season_id,
        rules_config_id=rules_config_id,
        recommendation_type=RecommendationType.JOINT_SQUAD_AND_XI,
        run_status=OptimizationRunStatus.SUCCEEDED,
        solver_status=SolverStatus.OPTIMAL,
        target_gameweek_id=gameweek.id,
        target_gameweek_status=TargetGameweekStatus.RESOLVED,
        captain_player_id=captain_player_id,
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
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    db_session.add(
        OptimizationRunPlayer(
            optimization_run_id=run.id,
            player_id=captain_player_id,
            is_starting=True,
            is_captain=True,
            is_vice_captain=False,
            projected_points=Decimal(str(projected_points)),
            projection_source="test",
            now_cost_snapshot=80,
        )
    )
    db_session.commit()
    return run


def _seed_actual_points(db_session, player_id, season_id, gameweek_id, total_points: int, minutes: int = 90):
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


def _seed_prior_degraded_gameweek(db_session, season_id, rules_config_id, fpl_event_id, model_version_id):
    """A prior gameweek's own audit/outcome, hand-built with the SAME degraded captain-projection
    pattern as the reviewed gameweek (see module docstring) -- feeds both
    ``ROLLING_PROJECTION_MAE`` (via ``OptimizationRunPlayer``/``player_gameweek_stats``) and
    ``FAILURE_REASON_CONCENTRATION`` (via a directly-persisted ``PROJECTION_OVERESTIMATED`` row --
    this job never itself reviews these earlier gameweeks, so their own attributions must be seeded
    directly, mirroring how a real earlier GAMEWEEK_REVIEW run would have produced them).
    """
    gameweek = _gameweek(db_session, season_id, fpl_event_id)
    captain_id = _player_id(db_session, season_id, _CAPTAIN_ELEMENT_ID)
    opt_run = _create_optimization_run_with_captain_projection(
        db_session, season_id, rules_config_id, gameweek, captain_id, _PROJECTED_POINTS
    )
    _seed_actual_points(db_session, captain_id, season_id, gameweek.id, _ACTUAL_RAW_POINTS)
    audit = _create_audit(
        db_session,
        season_id,
        gameweek,
        optimization_run_id=opt_run.id,
        champion_model_version_id=model_version_id,
    )
    outcome = _create_outcome(db_session, audit.id)
    persist_failure_attributions(
        db_session,
        outcome.id,
        [
            FailureAttributionData(
                reason_code=FailureReasonCode.PROJECTION_OVERESTIMATED,
                category=FailureCategory.PERFORMANCE_MODEL,
                severity=Decimal("13.00"),
                evidence={"player_id": str(captain_id)},
            )
        ],
    )


def _seed_reviewed_gameweek(db_session, season_id, rules_config_id, model_version_id) -> RecommendationAudit:
    """The gameweek GAMEWEEK_REVIEW itself will process -- an authoritative
    ``recommendation_audits`` row with ONLY ``optimization_run_id`` resolved (no transfer/H2H/chip
    component), so ``ingest_outcome_for_audit`` computes a real (if minimal) outcome with zero
    counterfactual_results, and the captain-projection check is the ONE thing that fires.
    """
    gameweek = _gameweek(db_session, season_id, _REVIEWED_FPL_EVENT_ID)
    captain_id = _player_id(db_session, season_id, _CAPTAIN_ELEMENT_ID)
    opt_run = _create_optimization_run_with_captain_projection(
        db_session, season_id, rules_config_id, gameweek, captain_id, _PROJECTED_POINTS
    )
    _seed_actual_points(db_session, captain_id, season_id, gameweek.id, _ACTUAL_RAW_POINTS)
    return _create_audit(
        db_session,
        season_id,
        gameweek,
        optimization_run_id=opt_run.id,
        champion_model_version_id=model_version_id,
    )


def _build_full_review_scenario(db_session) -> tuple[uuid.UUID, RecommendationAudit]:
    season_id = _seed_players(db_session)
    season = db_session.get(Season, season_id)
    rules_config_id = _seed_rules_config_id(db_session, season_id)
    model_version_id = _create_champion_model_version(db_session, season.label, _BASELINE_MAE)

    for fpl_event_id in _PRIOR_FPL_EVENT_IDS:
        _seed_prior_degraded_gameweek(db_session, season_id, rules_config_id, fpl_event_id, model_version_id)

    audit = _seed_reviewed_gameweek(db_session, season_id, rules_config_id, model_version_id)
    return season_id, audit


# --- Happy path -----------------------------------------------------------------------------------


def test_gameweek_review_happy_path_produces_succeeded_report_with_real_findings(
    db_session, patched_fpl_client_optimization
):
    season_id, audit = _build_full_review_scenario(db_session)

    job = execute_job(
        db_session,
        job_type=JobType.GAMEWEEK_REVIEW,
        parameters={"target_fpl_event_id": _REVIEWED_FPL_EVENT_ID, "season_id": str(season_id)},
        trigger_type=TriggerType.LOCAL,
    )

    assert job.status == JobStatus.SUCCEEDED, job.error_message
    learning_report_id = uuid.UUID(job.output_json["learning_report_id"])
    report = db_session.get(LearningReport, learning_report_id)
    assert report is not None
    assert report.run_status == GameweekReviewRunStatus.SUCCEEDED
    assert report.season_id == season_id
    assert report.completed_at is not None

    # --- recommendation_outcome_id set, pointing at a real, upserted-in-place outcome row ---
    assert report.recommendation_outcome_id is not None
    outcome = db_session.get(RecommendationOutcome, report.recommendation_outcome_id)
    assert outcome is not None
    assert outcome.recommendation_audit_id == audit.id

    # --- failure_attribution_count correct: exactly one PROJECTION_OVERESTIMATED for this gameweek ---
    assert report.failure_attribution_count == 1
    attributions = (
        db_session.execute(
            select(FailureAttribution).where(FailureAttribution.recommendation_outcome_id == outcome.id)
        )
        .scalars()
        .all()
    )
    assert len(attributions) == 1
    assert attributions[0].reason_code == FailureReasonCode.PROJECTION_OVERESTIMATED

    # --- all 4 signal types computed and persisted as real learning_signals rows ---
    persisted_signals = (
        db_session.execute(
            select(LearningSignal).where(
                LearningSignal.season_id == season_id,
                LearningSignal.as_of_gameweek_id == audit.target_gameweek_id,
            )
        )
        .scalars()
        .all()
    )
    signal_types_persisted = {s.signal_type for s in persisted_signals}
    assert signal_types_persisted == {
        LearningSignalType.ROLLING_PROJECTION_MAE,
        LearningSignalType.ROLLING_H2H_WIN_PROBABILITY_CALIBRATION,
        LearningSignalType.ROLLING_CHIP_RECOMMENDATION_VALUE,
        LearningSignalType.FAILURE_REASON_CONCENTRATION,
    }
    mae_signal = next(
        s for s in persisted_signals if s.signal_type == LearningSignalType.ROLLING_PROJECTION_MAE
    )
    assert mae_signal.threshold_breached is True
    concentration_signal = next(
        s for s in persisted_signals if s.signal_type == LearningSignalType.FAILURE_REASON_CONCENTRATION
    )
    assert concentration_signal.threshold_breached is True
    h2h_signal = next(
        s
        for s in persisted_signals
        if s.signal_type == LearningSignalType.ROLLING_H2H_WIN_PROBABILITY_CALIBRATION
    )
    assert h2h_signal.sample_count == 0  # no H2H context seeded -- computed, just empty

    # --- report_markdown non-empty and contains expected real values ---
    assert report.report_markdown
    assert f"GW{_REVIEWED_FPL_EVENT_ID}" in report.report_markdown
    assert "projection_overestimated" in report.report_markdown
    assert "rolling_projection_mae" in report.report_markdown

    # --- learning_signals_computed_json: compact summary keyed by signal type ---
    assert report.learning_signals_computed_json is not None
    assert set(report.learning_signals_computed_json.keys()) == {s.value for s in LearningSignalType}
    assert report.learning_signals_computed_json["rolling_projection_mae"]["threshold_breached"] is True

    # --- notable_findings_json: 1 material failure + 2 breached signals + 1 retraining-triggered
    # entry (Milestone 7 Layer 6: this scenario's own sustained ROLLING_PROJECTION_MAE breach ALSO
    # triggers a real retraining attempt -- see the dedicated Layer 6 tests below for the full
    # trigger/promotion assertions; this happy-path test only needs to know a 4th finding now exists) --
    assert report.notable_findings_json is not None
    findings_by_type = [f["type"] for f in report.notable_findings_json]
    assert findings_by_type.count("material_failure") == 1
    assert findings_by_type.count("signal_breached") == 2
    assert findings_by_type.count("retraining_triggered") == 1
    material_finding = next(f for f in report.notable_findings_json if f["type"] == "material_failure")
    assert material_finding["reason_code"] == "projection_overestimated"
    breached_signal_types = {
        f["signal_type"] for f in report.notable_findings_json if f["type"] == "signal_breached"
    }
    assert breached_signal_types == {"rolling_projection_mae", "failure_reason_concentration"}

    # --- summary_json ---
    assert report.summary_json["target_fpl_event_id"] == _REVIEWED_FPL_EVENT_ID
    assert report.summary_json["failure_attribution_count"] == 1
    assert report.summary_json["notable_finding_count"] == 4
    assert report.retraining_triggered is True

    # --- note-effectiveness tracking: no notes were seeded anywhere in this scenario, so this
    # must be a clean no-op (0 rows, no error, no noise in the report) -- see the dedicated
    # "Note-effectiveness outcome tracking" section below for the real-note happy path. ---
    assert report.summary_json["note_effectiveness_count"] == 0
    assert report.summary_json["note_effectiveness_error"] is None
    assert "no active availability-risk/role-boost notes" in report.report_markdown.lower()


# --- Refusal paths --------------------------------------------------------------------------------


def test_gameweek_review_unfinished_gameweek_refuses_cleanly_no_row(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    season_id = _seed_players(db_session)
    unfinished_gameweek = db_session.execute(
        select(Gameweek).where(Gameweek.season_id == season_id, Gameweek.is_current.is_(True))
    ).scalar_one()
    assert unfinished_gameweek.finished is False

    job = execute_job(
        db_session,
        job_type=JobType.GAMEWEEK_REVIEW,
        parameters={"target_fpl_event_id": unfinished_gameweek.fpl_event_id, "season_id": str(season_id)},
        trigger_type=TriggerType.LOCAL,
    )

    assert job.status == JobStatus.FAILED
    assert job.error_code == "GAMEWEEK_NOT_FINISHED"
    count = (
        db_session.execute(select(LearningReport).where(LearningReport.gameweek_id == unfinished_gameweek.id))
        .scalars()
        .all()
    )
    assert count == []


def test_gameweek_review_no_authoritative_recommendation_refuses_cleanly_no_row(
    db_session, patched_fpl_client_optimization
):
    season_id = _seed_players(db_session)
    gameweek = _gameweek(db_session, season_id, _REVIEWED_FPL_EVENT_ID)

    job = execute_job(
        db_session,
        job_type=JobType.GAMEWEEK_REVIEW,
        parameters={"target_fpl_event_id": _REVIEWED_FPL_EVENT_ID, "season_id": str(season_id)},
        trigger_type=TriggerType.LOCAL,
    )

    assert job.status == JobStatus.FAILED
    assert job.error_code == "NO_AUTHORITATIVE_RECOMMENDATION"
    rows = (
        db_session.execute(select(LearningReport).where(LearningReport.gameweek_id == gameweek.id))
        .scalars()
        .all()
    )
    assert rows == []


# --- Rerun behavior --------------------------------------------------------------------------------


def test_gameweek_review_rerun_creates_a_second_insert_only_report_row(
    db_session, patched_fpl_client_optimization
):
    """learning_reports is insert-only per run (see that model's own docstring) -- a rerun for the
    SAME gameweek creates a FRESH row, never updates the prior one, while Layer 2/3's own
    already-established idempotency (upsert-in-place for the outcome, delete-then-reinsert for its
    failure attributions) is respected, not fought against.
    """
    season_id, audit = _build_full_review_scenario(db_session)
    parameters = {"target_fpl_event_id": _REVIEWED_FPL_EVENT_ID, "season_id": str(season_id)}

    first_job = execute_job(
        db_session, job_type=JobType.GAMEWEEK_REVIEW, parameters=parameters, trigger_type=TriggerType.LOCAL
    )
    assert first_job.status == JobStatus.SUCCEEDED
    second_job = execute_job(
        db_session, job_type=JobType.GAMEWEEK_REVIEW, parameters=parameters, trigger_type=TriggerType.LOCAL
    )
    assert second_job.status == JobStatus.SUCCEEDED

    first_report_id = uuid.UUID(first_job.output_json["learning_report_id"])
    second_report_id = uuid.UUID(second_job.output_json["learning_report_id"])
    assert first_report_id != second_report_id

    reports = (
        db_session.execute(
            select(LearningReport).where(LearningReport.gameweek_id == audit.target_gameweek_id)
        )
        .scalars()
        .all()
    )
    assert len(reports) == 2
    assert {r.run_status for r in reports} == {GameweekReviewRunStatus.SUCCEEDED}

    # Both reports point at the SAME (upserted-in-place) recommendation_outcomes row.
    assert reports[0].recommendation_outcome_id == reports[1].recommendation_outcome_id

    # The outcome's own failure_attributions were deleted-and-reinserted, not accumulated.
    attributions = (
        db_session.execute(
            select(FailureAttribution).where(
                FailureAttribution.recommendation_outcome_id == reports[0].recommendation_outcome_id
            )
        )
        .scalars()
        .all()
    )
    assert len(attributions) == 1

    # Learning signals, in contrast, are NEVER upserted -- each computation is a fresh row, so a
    # rerun leaves 2x as many learning_signals rows for this exact (season, as_of_gameweek).
    signals = (
        db_session.execute(
            select(LearningSignal).where(
                LearningSignal.season_id == season_id,
                LearningSignal.as_of_gameweek_id == audit.target_gameweek_id,
            )
        )
        .scalars()
        .all()
    )
    assert len(signals) == 8  # 4 signal types x 2 runs


# --- Milestone 7 Layer 6: retraining-trigger wiring ------------------------------------------------


def test_gameweek_review_no_mae_breach_does_not_trigger_retraining(
    db_session, patched_fpl_client_optimization
):
    """A normal review (no sustained ROLLING_PROJECTION_MAE breach -- only 1 gameweek in the window,
    well under ``_MIN_CONSECUTIVE_DEGRADED_GAMEWEEKS=3``, so ``_is_sustained_breach`` cannot fire
    regardless of magnitude) never triggers a MODEL_TRAINING job.
    """
    season_id = _seed_players(db_session)
    season = db_session.get(Season, season_id)
    rules_config_id = _seed_rules_config_id(db_session, season_id)
    model_version_id = _create_champion_model_version(db_session, season.label, _BASELINE_MAE)
    audit = _seed_reviewed_gameweek(db_session, season_id, rules_config_id, model_version_id)

    def _model_training_job_count() -> int:
        return len(
            db_session.execute(select(Job).where(Job.job_type == JobType.MODEL_TRAINING)).scalars().all()
        )

    model_training_jobs_before = _model_training_job_count()

    job = execute_job(
        db_session,
        job_type=JobType.GAMEWEEK_REVIEW,
        parameters={"target_fpl_event_id": _REVIEWED_FPL_EVENT_ID, "season_id": str(season_id)},
        trigger_type=TriggerType.LOCAL,
    )

    assert job.status == JobStatus.SUCCEEDED, job.error_message
    learning_report_id = uuid.UUID(job.output_json["learning_report_id"])
    report = db_session.get(LearningReport, learning_report_id)
    assert report is not None

    mae_signal = db_session.execute(
        select(LearningSignal).where(
            LearningSignal.season_id == season_id,
            LearningSignal.as_of_gameweek_id == audit.target_gameweek_id,
            LearningSignal.signal_type == LearningSignalType.ROLLING_PROJECTION_MAE,
        )
    ).scalar_one()
    assert mae_signal.threshold_breached is False

    assert report.retraining_triggered is False
    assert report.retraining_job_id is None
    assert job.output_json["retraining_triggered"] is False
    assert job.output_json["retraining_job_id"] is None
    findings_by_type = [f["type"] for f in report.notable_findings_json]
    assert "retraining_triggered" not in findings_by_type

    # No new MODEL_TRAINING job was created as a side effect.
    assert _model_training_job_count() == model_training_jobs_before


def test_gameweek_review_sustained_mae_breach_triggers_model_training_and_shadow_promotion_check(
    db_session, patched_fpl_client_optimization, patched_historical_fpl_client
):
    """A genuine, sustained ROLLING_PROJECTION_MAE breach triggers a real MODEL_TRAINING job; since a
    real global champion already exists, that training run's own winner becomes a SHADOW_CHALLENGER
    (never an immediate new champion -- Layer 5's own non-bootstrap branch), which this handler then
    immediately runs a promotion-gate check against -- with zero prior shadow-evaluation history,
    that always comes back RETAINED_INCONCLUSIVE (see app.modeling.promotion's own module docstring),
    which is exactly what's asserted here: nothing special-cased, just the real gate function called
    and its real (inconclusive) verdict recorded.
    """
    _seed_historical_season_for_model_training(db_session)

    season_id = _seed_players(db_session)
    season = db_session.get(Season, season_id)
    rules_config_id = _seed_rules_config_id(db_session, season_id)
    champion_id = _create_global_champion_model_version(db_session, season.label, _BASELINE_MAE)

    for fpl_event_id in _PRIOR_FPL_EVENT_IDS:
        _seed_prior_degraded_gameweek(db_session, season_id, rules_config_id, fpl_event_id, champion_id)
    audit = _seed_reviewed_gameweek(db_session, season_id, rules_config_id, champion_id)

    job = execute_job(
        db_session,
        job_type=JobType.GAMEWEEK_REVIEW,
        parameters={"target_fpl_event_id": _REVIEWED_FPL_EVENT_ID, "season_id": str(season_id)},
        trigger_type=TriggerType.LOCAL,
    )

    assert job.status == JobStatus.SUCCEEDED, job.error_message
    learning_report_id = uuid.UUID(job.output_json["learning_report_id"])
    report = db_session.get(LearningReport, learning_report_id)
    assert report is not None

    mae_signal = db_session.execute(
        select(LearningSignal).where(
            LearningSignal.season_id == season_id,
            LearningSignal.as_of_gameweek_id == audit.target_gameweek_id,
            LearningSignal.signal_type == LearningSignalType.ROLLING_PROJECTION_MAE,
        )
    ).scalar_one()
    assert mae_signal.threshold_breached is True

    # --- retraining_triggered=True, retraining_job_id points at a real, SUCCEEDED MODEL_TRAINING job ---
    assert report.retraining_triggered is True
    assert report.retraining_job_id is not None
    training_job = db_session.get(Job, report.retraining_job_id)
    assert training_job is not None
    assert training_job.job_type == JobType.MODEL_TRAINING
    assert training_job.status == JobStatus.SUCCEEDED
    assert training_job.output_json["champion"]["promotion_outcome"] == "shadow_challenger"

    shadow_challenger_id = uuid.UUID(training_job.output_json["champion"]["model_version_id"])
    shadow_challenger = db_session.get(ModelVersion, shadow_challenger_id)
    assert shadow_challenger.lifecycle_state == ModelLifecycleState.SHADOW_CHALLENGER
    assert shadow_challenger.is_champion is False

    # The pre-existing global champion is untouched.
    champion = db_session.get(ModelVersion, champion_id)
    assert champion.is_champion is True
    assert champion.lifecycle_state == ModelLifecycleState.CHAMPION

    # --- a real, RETAINED_INCONCLUSIVE ModelPromotion row was recorded for this exact pair ---
    promotions = (
        db_session.execute(select(ModelPromotion).where(ModelPromotion.decided_by_job_id == job.id))
        .scalars()
        .all()
    )
    assert len(promotions) == 1
    assert promotions[0].verdict == PromotionVerdict.RETAINED_INCONCLUSIVE
    assert promotions[0].challenger_model_version_id == shadow_challenger_id

    assert job.output_json["retraining_triggered"] is True
    assert job.output_json["retraining_promotion_verdict"] == "retained_inconclusive"

    # --- notable_findings_json/report_markdown fold in the retraining outcome ---
    retraining_finding = next(f for f in report.notable_findings_json if f["type"] == "retraining_triggered")
    assert retraining_finding["model_training_status"] == "succeeded"
    assert retraining_finding["promotion_verdict"] == "retained_inconclusive"
    assert "Retraining trigger" in report.report_markdown
    assert str(training_job.id) in report.report_markdown


def test_gameweek_review_retraining_failure_is_recorded_but_never_fails_the_review(
    db_session, patched_fpl_client_optimization
):
    """A sustained MAE breach still triggers a MODEL_TRAINING attempt even when that nested training
    run itself would fail (here: no player_features exist anywhere for the champion's own
    feature_version, so MODEL_TRAINING refuses with NO_SEASONS_WITH_FEATURES) -- GAMEWEEK_REVIEW's
    own primary work (outcome ingestion, failure attribution, the 4 signals) already succeeded by
    this point, so the overall job result must still be SUCCEEDED, with the retraining failure
    recorded on the report, never propagated as this run's own failure.
    """
    season_id = _seed_players(db_session)
    season = db_session.get(Season, season_id)
    rules_config_id = _seed_rules_config_id(db_session, season_id)
    champion_id = _create_global_champion_model_version(
        db_session, season.label, _BASELINE_MAE, feature_version=_NO_FEATURES_SENTINEL_FEATURE_VERSION
    )

    for fpl_event_id in _PRIOR_FPL_EVENT_IDS:
        _seed_prior_degraded_gameweek(db_session, season_id, rules_config_id, fpl_event_id, champion_id)
    audit = _seed_reviewed_gameweek(db_session, season_id, rules_config_id, champion_id)

    job = execute_job(
        db_session,
        job_type=JobType.GAMEWEEK_REVIEW,
        parameters={"target_fpl_event_id": _REVIEWED_FPL_EVENT_ID, "season_id": str(season_id)},
        trigger_type=TriggerType.LOCAL,
    )

    # --- the review itself still reports SUCCEEDED overall ---
    assert job.status == JobStatus.SUCCEEDED, job.error_message
    learning_report_id = uuid.UUID(job.output_json["learning_report_id"])
    report = db_session.get(LearningReport, learning_report_id)
    assert report is not None
    assert report.run_status == GameweekReviewRunStatus.SUCCEEDED

    mae_signal = db_session.execute(
        select(LearningSignal).where(
            LearningSignal.season_id == season_id,
            LearningSignal.as_of_gameweek_id == audit.target_gameweek_id,
            LearningSignal.signal_type == LearningSignalType.ROLLING_PROJECTION_MAE,
        )
    ).scalar_one()
    assert mae_signal.threshold_breached is True

    # --- the retraining ATTEMPT is recorded, and its own failure is visible, never masked ---
    assert report.retraining_triggered is True
    assert report.retraining_job_id is not None
    training_job = db_session.get(Job, report.retraining_job_id)
    assert training_job is not None
    assert training_job.job_type == JobType.MODEL_TRAINING
    assert training_job.status == JobStatus.FAILED
    assert training_job.error_code == "NO_SEASONS_WITH_FEATURES"

    retraining_finding = next(f for f in report.notable_findings_json if f["type"] == "retraining_triggered")
    assert retraining_finding["model_training_status"] == "failed"
    assert retraining_finding["error"] is not None
    assert retraining_finding["promotion_verdict"] is None

    # --- no promotion-gate check ever ran (training never succeeded, so no SHADOW_CHALLENGER exists) ---
    promotions = (
        db_session.execute(select(ModelPromotion).where(ModelPromotion.decided_by_job_id == job.id))
        .scalars()
        .all()
    )
    assert promotions == []


# --- Milestone: price-signal auto-recalibration -----------------------------------------------
# app.jobs.handlers.gameweek_review.GameweekReviewJobRunner._maybe_recalibrate_price_signal --
# independent of the MAE-breach retraining trigger above; runs on every review regardless.


def test_gameweek_review_price_signal_recalibration_skipped_when_under_sample_size(
    db_session, patched_fpl_client_optimization
):
    """_seed_players' own single DATA_INGESTION run leaves exactly one observation per player (no
    "next" observation yet to pair against) -- nowhere near
    price_signal_calibration.MIN_CALIBRATION_SAMPLE_SIZE. This is the overwhelmingly common state
    for most of a season, so it gets its own dedicated happy-path-shaped test, mirroring
    test_gameweek_review_no_mae_breach_does_not_trigger_retraining's exact shape.
    """
    # Shared, persistent local Postgres (see this module's own docstring) -- explicitly count
    # rows before/after rather than assert zero-ever, since an earlier test in this same run (or
    # an earlier full-suite pass, per this repo's own "run integration twice" protocol) may have
    # already left real price_signal_calibrations rows behind (append-only, by design).
    calibration_count_before = db_session.execute(
        select(func.count()).select_from(PriceSignalCalibration)
    ).scalar_one()

    season_id = _seed_players(db_session)
    season = db_session.get(Season, season_id)
    rules_config_id = _seed_rules_config_id(db_session, season_id)
    model_version_id = _create_champion_model_version(db_session, season.label, _BASELINE_MAE)
    _seed_reviewed_gameweek(db_session, season_id, rules_config_id, model_version_id)

    job = execute_job(
        db_session,
        job_type=JobType.GAMEWEEK_REVIEW,
        parameters={"target_fpl_event_id": _REVIEWED_FPL_EVENT_ID, "season_id": str(season_id)},
        trigger_type=TriggerType.LOCAL,
    )

    assert job.status == JobStatus.SUCCEEDED, job.error_message
    assert job.output_json["price_signal_recalibration_triggered"] is False
    assert job.output_json["price_signal_new_ratio_threshold"] is None
    assert job.output_json["price_signal_new_absolute_floor"] is None

    calibration_count_after = db_session.execute(
        select(func.count()).select_from(PriceSignalCalibration)
    ).scalar_one()
    assert calibration_count_after == calibration_count_before


def test_gameweek_review_price_signal_recalibration_triggers_and_persists_new_calibration(
    db_session, patched_fpl_client_optimization
):
    """Seeds enough real (signal, outcome) evidence -- a clear, large, repeated momentum spike
    that always precedes an observed price change, for one real player -- to cross
    MIN_CALIBRATION_SAMPLE_SIZE, and asserts a new, active price_signal_calibrations row is
    persisted with the expected sample size and a real accuracy score.
    """
    # Shared, persistent local Postgres -- explicitly clear any pre-existing active row so this
    # test's own tie-break/accuracy assertions are deterministic regardless of what earlier
    # tests/runs left behind (same convention used elsewhere in this module for is_champion).
    db_session.execute(update(PriceSignalCalibration).values(is_active=False))
    db_session.commit()

    season_id = _seed_players(db_session)
    season = db_session.get(Season, season_id)
    rules_config_id = _seed_rules_config_id(db_session, season_id)
    model_version_id = _create_champion_model_version(db_session, season.label, _BASELINE_MAE)
    _seed_reviewed_gameweek(db_session, season_id, rules_config_id, model_version_id)

    # _seed_players' own DATA_INGESTION call unconditionally records ONE player_price_signal_observations
    # row per player (see app.ingestion.normalization.record_price_signal_observations -- runs every
    # ingestion, "a quiet gameweek is just as necessary for calibration as a real spike"), at the real
    # wall-clock time the test happens to run. Left in place, that extra observation for the captain
    # would form one more (player_id, observed_at)-consecutive pair beyond the exact
    # MIN_CALIBRATION_SAMPLE_SIZE this test seeds below, making `price_signal_recalibration_sample_size`
    # depend on real time relative to `base_time` -- delete it so this test has full, deterministic
    # control over every observation row that exists for this season.
    db_session.execute(
        delete(PlayerPriceSignalObservation).where(PlayerPriceSignalObservation.season_id == season_id)
    )
    db_session.commit()

    captain_id = _player_id(db_session, season_id, _CAPTAIN_ELEMENT_ID)
    base_time = datetime(2026, 8, 1, tzinfo=UTC)
    observation_count = MIN_CALIBRATION_SAMPLE_SIZE + 1  # yields exactly MIN_CALIBRATION_SAMPLE_SIZE pairs
    for i in range(observation_count):
        db_session.add(
            PlayerPriceSignalObservation(
                player_id=captain_id,
                season_id=season_id,
                observed_at=base_time + timedelta(hours=i),
                transfers_in_event=50_000,
                transfers_out_event=0,
                selected_by_percent=Decimal("10.0"),
                total_players=1_000_000,
            )
        )
        # A real price change strictly between every consecutive pair of observations -- every
        # one of the MIN_CALIBRATION_SAMPLE_SIZE samples this produces has price_changed=True.
        # Pair i spans (obs[i], obs[i+1]) = (base+i*1h, base+(i+1)*1h), so the change for pair i
        # must land at base+i*1h+30min -- guarded to i < observation_count - 1 (one fewer change
        # than observations, matching one fewer pair than observations).
        if i < observation_count - 1:
            db_session.add(
                PlayerPriceHistory(
                    player_id=captain_id,
                    season_id=season_id,
                    old_cost=50 + i,
                    new_cost=51 + i,
                    observed_at=base_time + timedelta(hours=i, minutes=30),
                )
            )
    db_session.commit()

    job = execute_job(
        db_session,
        job_type=JobType.GAMEWEEK_REVIEW,
        parameters={"target_fpl_event_id": _REVIEWED_FPL_EVENT_ID, "season_id": str(season_id)},
        trigger_type=TriggerType.LOCAL,
    )

    assert job.status == JobStatus.SUCCEEDED, job.error_message
    assert job.output_json["price_signal_recalibration_triggered"] is True
    assert job.output_json["price_signal_recalibration_sample_size"] == MIN_CALIBRATION_SAMPLE_SIZE

    active_calibrations = (
        db_session.execute(select(PriceSignalCalibration).where(PriceSignalCalibration.is_active.is_(True)))
        .scalars()
        .all()
    )
    assert len(active_calibrations) == 1
    assert active_calibrations[0].sample_size == MIN_CALIBRATION_SAMPLE_SIZE
    assert active_calibrations[0].accuracy == Decimal("1.0000")


def test_gameweek_review_price_signal_recalibration_failure_is_recorded_but_never_fails_the_review(
    db_session, patched_fpl_client_optimization, monkeypatch
):
    """A recalibration-step failure (here: forced via monkeypatch, mirroring
    test_gameweek_review_retraining_failure_is_recorded_but_never_fails_the_review's own
    approach for a case that's otherwise hard to trigger with real seeded data alone) must never
    fail GAMEWEEK_REVIEW's own already-succeeded primary work.
    """

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated calibration failure")

    monkeypatch.setattr("app.jobs.handlers.gameweek_review.calibrate_thresholds", _boom)

    season_id = _seed_players(db_session)
    season = db_session.get(Season, season_id)
    rules_config_id = _seed_rules_config_id(db_session, season_id)
    model_version_id = _create_champion_model_version(db_session, season.label, _BASELINE_MAE)
    _seed_reviewed_gameweek(db_session, season_id, rules_config_id, model_version_id)

    job = execute_job(
        db_session,
        job_type=JobType.GAMEWEEK_REVIEW,
        parameters={"target_fpl_event_id": _REVIEWED_FPL_EVENT_ID, "season_id": str(season_id)},
        trigger_type=TriggerType.LOCAL,
    )

    assert job.status == JobStatus.SUCCEEDED, job.error_message
    learning_report_id = uuid.UUID(job.output_json["learning_report_id"])
    report = db_session.get(LearningReport, learning_report_id)
    assert report.run_status == GameweekReviewRunStatus.SUCCEEDED
    assert job.output_json["price_signal_recalibration_triggered"] is False


# --- Note-effectiveness outcome tracking wiring ----------------------------------------------------
#
# Focused, deterministic unit-of-work coverage for evaluate_note_effectiveness_for_outcome itself
# (every boundary condition: no notes, expired notes, inclusive gameweek-window edges, stacked
# notes, reruns) lives in tests/integration/test_note_effectiveness.py -- these tests exist only to
# prove the SEQUENCING/COMPOSITION into a real GAMEWEEK_REVIEW run works end to end, mirroring this
# module's own split between "does the building block work" (covered elsewhere) and "is it wired up
# correctly here" (this file's actual job, per its own module docstring).


def test_gameweek_review_tracks_note_effectiveness_for_a_player_with_an_active_risk_note(
    db_session, patched_fpl_client_optimization
):
    """A player (not the captain, so this is independent of the MAE-breach scenario) with an
    active availability-risk note applied to their recommended-squad projection: seeded to
    actually deliver their FULL un-discounted total this gameweek, so the note demonstrably moved
    the projection FURTHER from the real outcome -- the one case that must show up as a notable
    finding, not just a quiet row in the DB.
    """
    season_id = _seed_players(db_session)
    season = db_session.get(Season, season_id)
    rules_config_id = _seed_rules_config_id(db_session, season_id)
    model_version_id = _create_champion_model_version(db_session, season.label, _BASELINE_MAE)
    audit = _seed_reviewed_gameweek(db_session, season_id, rules_config_id, model_version_id)
    gameweek = _gameweek(db_session, season_id, _REVIEWED_FPL_EVENT_ID)

    noted_player_id = _player_id(db_session, season_id, _CAPTAIN_ELEMENT_ID + 1)
    db_session.add(
        OptimizationRunPlayer(
            optimization_run_id=audit.optimization_run_id,
            player_id=noted_player_id,
            is_starting=True,
            is_captain=False,
            is_vice_captain=False,
            projected_points=Decimal("6.50"),  # 10.0 base * 0.65 severity
            projection_source=f"test{AVAILABILITY_RISK_NOTED_SUFFIX}",
            now_cost_snapshot=45,
        )
    )
    db_session.add(
        PlayerAvailabilityRiskNote(
            player_id=noted_player_id,
            season_id=season_id,
            reason="test risk note",
            severity=Decimal("0.65"),
            valid_from_fpl_event_id=1,
            valid_until_fpl_event_id=None,
            created_by="test",
        )
    )
    db_session.commit()
    _seed_actual_points(db_session, noted_player_id, season_id, gameweek.id, 10)

    job = execute_job(
        db_session,
        job_type=JobType.GAMEWEEK_REVIEW,
        parameters={"target_fpl_event_id": _REVIEWED_FPL_EVENT_ID, "season_id": str(season_id)},
        trigger_type=TriggerType.LOCAL,
    )

    assert job.status == JobStatus.SUCCEEDED, job.error_message
    assert job.output_json["note_effectiveness_count"] == 1

    report_id = uuid.UUID(job.output_json["learning_report_id"])
    report = db_session.get(LearningReport, report_id)
    assert report.summary_json["note_effectiveness_count"] == 1
    assert report.summary_json["note_effectiveness_negative_count"] == 1
    assert "Note effectiveness" in report.report_markdown
    assert "availability_risk" in report.report_markdown

    findings_by_type = [f["type"] for f in report.notable_findings_json]
    assert "note_effectiveness_negative" in findings_by_type
    negative_finding = next(
        f for f in report.notable_findings_json if f["type"] == "note_effectiveness_negative"
    )
    assert negative_finding["note_type"] == "availability_risk"

    persisted = (
        db_session.execute(
            select(NoteEffectivenessEvaluation).where(
                NoteEffectivenessEvaluation.recommendation_outcome_id == report.recommendation_outcome_id
            )
        )
        .scalars()
        .all()
    )
    assert len(persisted) == 1
    assert persisted[0].player_id == noted_player_id
    assert persisted[0].moved_closer_to_actual is False
    assert persisted[0].projected_points_without_note == Decimal("10.00")


def test_gameweek_review_note_effectiveness_failure_is_recorded_but_never_fails_the_review(
    db_session, patched_fpl_client_optimization, monkeypatch
):
    """A note-effectiveness-tracking failure (forced via monkeypatch, mirroring
    test_gameweek_review_price_signal_recalibration_failure_is_recorded_but_never_fails_the_review's
    own approach) must never fail GAMEWEEK_REVIEW's own already-succeeded primary work -- this is
    additive, informational tracking layered on top, exactly like price-signal recalibration.
    """

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated note-effectiveness failure")

    monkeypatch.setattr("app.jobs.handlers.gameweek_review.evaluate_note_effectiveness_for_outcome", _boom)

    season_id = _seed_players(db_session)
    season = db_session.get(Season, season_id)
    rules_config_id = _seed_rules_config_id(db_session, season_id)
    model_version_id = _create_champion_model_version(db_session, season.label, _BASELINE_MAE)
    _seed_reviewed_gameweek(db_session, season_id, rules_config_id, model_version_id)

    job = execute_job(
        db_session,
        job_type=JobType.GAMEWEEK_REVIEW,
        parameters={"target_fpl_event_id": _REVIEWED_FPL_EVENT_ID, "season_id": str(season_id)},
        trigger_type=TriggerType.LOCAL,
    )

    assert job.status == JobStatus.SUCCEEDED, job.error_message
    learning_report_id = uuid.UUID(job.output_json["learning_report_id"])
    report = db_session.get(LearningReport, learning_report_id)
    assert report.run_status == GameweekReviewRunStatus.SUCCEEDED
    assert report.summary_json["note_effectiveness_count"] == 0
    assert report.summary_json["note_effectiveness_error"] == "simulated note-effectiveness failure"
