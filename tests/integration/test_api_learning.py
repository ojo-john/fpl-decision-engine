"""Integration tests for the /jobs/review-gameweek, /jobs/backtest, and /learning endpoints against
a real database.

Mirrors ``test_api_recommendations.py``'s own "duplicate a minimal setup
rather than import from the job-level test module" discipline -- this module
tests the HTTP layer specifically (request/response shape, error-to-409/404/400
mapping, query-param filtering), not the jobs' own orchestration logic, which
is already covered in depth by ``test_gameweek_review_job.py``/``test_backtest_job.py``.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.jobs.service import execute_job
from apps.api.main import create_app
from packages.database.models.gameweek import Gameweek
from packages.database.models.model_version import ModelVersion
from packages.database.models.optimization_run import OptimizationRun
from packages.database.models.optimization_run_player import OptimizationRunPlayer
from packages.database.models.player import Player
from packages.database.models.player_gameweek_stats import PlayerGameweekStats
from packages.database.models.recommendation_audit import RecommendationAudit
from packages.database.models.rules_config import RulesConfig
from packages.database.models.season import Season
from packages.domain.jobs import JobStatus, JobType, TriggerType
from packages.domain.learning import RecommendationConfidence
from packages.domain.optimization import (
    OptimizationRunStatus,
    RecommendationType,
    SolverStatus,
    TargetGameweekStatus,
)
from packages.domain.rules import RulesStatus

_REVIEWED_FPL_EVENT_ID = 20
_CAPTAIN_ELEMENT_ID = 30000


def _now():
    return datetime.now(UTC)


def _seed_season(db_session) -> uuid.UUID:
    ingestion_job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert ingestion_job.status == JobStatus.SUCCEEDED
    return db_session.execute(
        select(Season.id).where(Season.label == ingestion_job.output_json["season_label"])
    ).scalar_one()


def _seed_authoritative_audit_with_material_captain_miss(db_session, season_id) -> RecommendationAudit:
    """Minimal scenario: one gameweek with an authoritative recommendation whose recommended captain
    was materially overestimated (15.0 projected vs. 2.0 real raw points) -- enough for
    GAMEWEEK_REVIEW to produce a real PROJECTION_OVERESTIMATED failure attribution and 4 real
    learning_signals rows (some with sample_count=0, still genuinely "computed"). See
    ``tests/integration/test_gameweek_review_job.py``'s own, richer version of this same pattern.
    """
    gameweek = db_session.execute(
        select(Gameweek).where(
            Gameweek.season_id == season_id, Gameweek.fpl_event_id == _REVIEWED_FPL_EVENT_ID
        )
    ).scalar_one()
    rules_config_id = db_session.execute(
        select(RulesConfig.id).where(RulesConfig.season_id == season_id, RulesConfig.is_current.is_(True))
    ).scalar_one()
    captain_id = db_session.execute(
        select(Player.id).where(Player.season_id == season_id, Player.fpl_element_id == _CAPTAIN_ELEMENT_ID)
    ).scalar_one()

    run = OptimizationRun(
        season_id=season_id,
        rules_config_id=rules_config_id,
        recommendation_type=RecommendationType.JOINT_SQUAD_AND_XI,
        run_status=OptimizationRunStatus.SUCCEEDED,
        solver_status=SolverStatus.OPTIMAL,
        target_gameweek_id=gameweek.id,
        target_gameweek_status=TargetGameweekStatus.RESOLVED,
        captain_player_id=captain_id,
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
            player_id=captain_id,
            is_starting=True,
            is_captain=True,
            is_vice_captain=False,
            projected_points=Decimal("15.00"),
            projection_source="test",
            now_cost_snapshot=80,
        )
    )
    db_session.commit()

    db_session.add(
        PlayerGameweekStats(
            player_id=captain_id,
            season_id=season_id,
            gameweek_id=gameweek.id,
            minutes=90,
            total_points=2,
            data_source="test",
        )
    )
    db_session.commit()

    audit = RecommendationAudit(
        season_id=season_id,
        target_gameweek_id=gameweek.id,
        target_fpl_event_id=gameweek.fpl_event_id,
        is_authoritative=True,
        optimization_run_id=run.id,
        confidence=RecommendationConfidence.HIGH,
        confidence_drivers_json=[],
        assumptions_json=[],
        known_risks_json=[],
        generated_at=_now(),
    )
    db_session.add(audit)
    db_session.commit()
    db_session.refresh(audit)
    return audit


def test_review_gameweek_happy_path(db_session, patched_fpl_client_optimization):
    season_id = _seed_season(db_session)
    _seed_authoritative_audit_with_material_captain_miss(db_session, season_id)
    client = TestClient(create_app())

    response = client.post("/jobs/review-gameweek", json={"target_fpl_event_id": _REVIEWED_FPL_EVENT_ID})

    assert response.status_code == 200
    body = response.json()
    assert body["run_status"] == "succeeded"
    assert body["target_fpl_event_id"] == _REVIEWED_FPL_EVENT_ID
    assert body["failure_attribution_count"] == 1
    assert body["recommendation_outcome_id"] is not None
    assert body["report_markdown"]
    # Milestone 7 Layer 6: retraining_triggered/retraining_job_id are surfaced on the response --
    # this minimal one-gameweek scenario has too little rolling history to ever breach
    # ROLLING_PROJECTION_MAE's own sustained-window threshold, so both stay at their "not triggered"
    # values (present, just not True/set) -- see test_gameweek_review_job.py's own dedicated Layer 6
    # scenarios for the triggered case.
    assert body["retraining_triggered"] is False
    assert body["retraining_job_id"] is None
    assert len(body["notable_findings"]) >= 1
    assert any(f["type"] == "material_failure" for f in body["notable_findings"])


def test_review_gameweek_no_authoritative_recommendation_returns_409(
    db_session, patched_fpl_client_optimization
):
    _seed_season(db_session)
    client = TestClient(create_app())

    response = client.post("/jobs/review-gameweek", json={"target_fpl_event_id": _REVIEWED_FPL_EVENT_ID})

    assert response.status_code == 409
    body = response.json()
    assert body["error_code"] == "NO_AUTHORITATIVE_RECOMMENDATION"
    assert "job_id" in body


def test_review_gameweek_unfinished_gameweek_returns_409(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    season_id = _seed_season(db_session)
    unfinished_gameweek = db_session.execute(
        select(Gameweek).where(Gameweek.season_id == season_id, Gameweek.is_current.is_(True))
    ).scalar_one()
    client = TestClient(create_app())

    response = client.post(
        "/jobs/review-gameweek", json={"target_fpl_event_id": unfinished_gameweek.fpl_event_id}
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "GAMEWEEK_NOT_FINISHED"


def test_get_learning_report_by_id_happy_path(db_session, patched_fpl_client_optimization):
    season_id = _seed_season(db_session)
    _seed_authoritative_audit_with_material_captain_miss(db_session, season_id)
    client = TestClient(create_app())

    generate_response = client.post(
        "/jobs/review-gameweek", json={"target_fpl_event_id": _REVIEWED_FPL_EVENT_ID}
    )
    assert generate_response.status_code == 200
    learning_report_id = generate_response.json()["learning_report_id"]

    get_response = client.get(f"/learning/reports/{learning_report_id}")

    assert get_response.status_code == 200
    assert get_response.json()["learning_report_id"] == learning_report_id


def test_get_learning_report_nonexistent_returns_404(db_session):
    client = TestClient(create_app())

    response = client.get(f"/learning/reports/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["error_code"] == "LEARNING_REPORT_NOT_FOUND"


def test_get_latest_learning_report_happy_path(db_session, patched_fpl_client_optimization):
    season_id = _seed_season(db_session)
    _seed_authoritative_audit_with_material_captain_miss(db_session, season_id)
    client = TestClient(create_app())

    generate_response = client.post(
        "/jobs/review-gameweek", json={"target_fpl_event_id": _REVIEWED_FPL_EVENT_ID}
    )
    assert generate_response.status_code == 200
    learning_report_id = generate_response.json()["learning_report_id"]

    latest_response = client.get("/learning/latest")

    assert latest_response.status_code == 200
    assert latest_response.json()["learning_report_id"] == learning_report_id


def test_get_latest_learning_report_none_exists_returns_404(db_session, patched_fpl_client_optimization):
    _seed_season(db_session)
    client = TestClient(create_app())

    response = client.get("/learning/latest")

    assert response.status_code == 404
    assert response.json()["error_code"] == "LEARNING_REPORT_NOT_FOUND"


def test_list_learning_signals_filters_by_signal_type(db_session, patched_fpl_client_optimization):
    season_id = _seed_season(db_session)
    _seed_authoritative_audit_with_material_captain_miss(db_session, season_id)
    client = TestClient(create_app())
    review_response = client.post(
        "/jobs/review-gameweek", json={"target_fpl_event_id": _REVIEWED_FPL_EVENT_ID}
    )
    assert review_response.status_code == 200

    all_response = client.get("/learning/signals")
    assert all_response.status_code == 200
    assert len(all_response.json()) >= 4

    filtered_response = client.get("/learning/signals", params={"signal_type": "rolling_projection_mae"})
    assert filtered_response.status_code == 200
    filtered_body = filtered_response.json()
    assert len(filtered_body) >= 1
    assert all(row["signal_type"] == "rolling_projection_mae" for row in filtered_body)


def test_list_learning_signals_invalid_signal_type_returns_400(db_session):
    client = TestClient(create_app())

    response = client.get("/learning/signals", params={"signal_type": "not_a_real_signal_type"})

    assert response.status_code == 400


def test_list_failure_attributions_filters_by_reason_code(db_session, patched_fpl_client_optimization):
    # /learning/failures is deliberately global, not season-scoped (see apps.api.routers.learning),
    # and this suite runs against a shared, non-truncated-between-tests Postgres -- so other tests'
    # own failure_attributions rows may legitimately already exist here. Assertions below check that
    # THIS test's own outcome shows up correctly filtered, not an exact global count.
    season_id = _seed_season(db_session)
    _seed_authoritative_audit_with_material_captain_miss(db_session, season_id)
    client = TestClient(create_app())
    review_response = client.post(
        "/jobs/review-gameweek", json={"target_fpl_event_id": _REVIEWED_FPL_EVENT_ID}
    )
    assert review_response.status_code == 200
    recommendation_outcome_id = review_response.json()["recommendation_outcome_id"]

    filtered_response = client.get("/learning/failures", params={"reason_code": "projection_overestimated"})
    assert filtered_response.status_code == 200
    filtered_body = filtered_response.json()
    assert all(row["reason_code"] == "projection_overestimated" for row in filtered_body)
    own_row = next(
        row for row in filtered_body if row["recommendation_outcome_id"] == recommendation_outcome_id
    )
    assert own_row["category"] == "performance_model"

    other_reason_response = client.get("/learning/failures", params={"reason_code": "chip_timing_suboptimal"})
    assert other_reason_response.status_code == 200
    assert all(
        row["recommendation_outcome_id"] != recommendation_outcome_id for row in other_reason_response.json()
    )


def test_list_failure_attributions_invalid_category_returns_400(db_session):
    client = TestClient(create_app())

    response = client.get("/learning/failures", params={"category": "not_a_real_category"})

    assert response.status_code == 400


# --- POST /jobs/backtest (Milestone 7 Layer 6) ------------------------------------------------------


def _seed_trained_model_version_id(db_session, patched_historical_fpl_client) -> uuid.UUID:
    """A real, already-trained ``model_versions`` row via a genuine HISTORICAL_DATA_INGESTION ->
    FEATURE_GENERATION -> MODEL_TRAINING pipeline -- duplicated (not imported) from
    ``tests/integration/test_backtest_job.py``'s own identically-shaped helper, per this repo's own
    "duplicate test setup rather than cross-import between test modules" convention.
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
    training_job = execute_job(
        db_session,
        job_type=JobType.MODEL_TRAINING,
        parameters={"feature_version": 1},
        trigger_type=TriggerType.LOCAL,
    )
    assert training_job.status == JobStatus.SUCCEEDED
    champion_id = uuid.UUID(training_job.output_json["champion"]["model_version_id"])
    return champion_id


def test_run_backtest_happy_path(db_session, patched_historical_fpl_client):
    model_version_id = _seed_trained_model_version_id(db_session, patched_historical_fpl_client)
    model_version = db_session.get(ModelVersion, model_version_id)
    client = TestClient(create_app())

    response = client.post("/jobs/backtest", json={"model_version_id": str(model_version_id)})

    assert response.status_code == 200
    body = response.json()
    assert body["model_version_id"] == str(model_version_id)
    assert body["model_family"] == model_version.model_family.value
    assert body["feature_version"] == 1
    assert body["run_status"] == "succeeded"
    assert body["usable_seasons"] == ["2023-24"]
    assert body["fold_count"] == 1
    assert body["degenerate_single_season"] is True
    assert len(body["fold_metrics"]) == 1
    assert body["aggregate_mae"] is not None
    assert float(body["aggregate_mae"]) >= 0
    assert body["n_observations"] > 0
    assert body["started_at"]
    assert body["completed_at"]


def test_run_backtest_model_version_not_found_returns_409(db_session):
    client = TestClient(create_app())

    response = client.post("/jobs/backtest", json={"model_version_id": str(uuid.uuid4())})

    assert response.status_code == 409
    body = response.json()
    assert body["error_code"] == "MODEL_VERSION_NOT_FOUND"
    assert "job_id" in body


def test_run_backtest_invalid_request_body_returns_422(db_session):
    client = TestClient(create_app())

    response = client.post("/jobs/backtest", json={})

    assert response.status_code == 422
