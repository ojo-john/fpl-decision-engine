"""Integration tests for GET /models/champion against a real database.

Mirrors test_api_learning.py's own _seed_trained_model_version_id helper
(duplicated, not imported -- this repo's established "duplicate test setup
across HTTP-layer test modules" convention) to get a real champion
model_versions row via a genuine HISTORICAL_DATA_INGESTION -> FEATURE_GENERATION
-> MODEL_TRAINING pipeline.
"""

import uuid

import pytest
from sqlalchemy import delete, select, update

from app.jobs.service import execute_job
from packages.database.models.model_version import ModelVersion
from packages.database.models.player_features import PlayerFeatures
from packages.database.models.season import Season
from packages.domain.jobs import JobStatus, JobType, TriggerType
from tests.integration.conftest import authed_client


def _clear_existing_champion(db_session) -> None:
    """Blanket reset across the WHOLE model_versions table -- is_champion has no season scoping
    of its own (see that model's own docstring), so a champion left behind by another test module
    in this same shared, non-truncated Postgres is otherwise indistinguishable from a real one.
    Mirrors test_champion_loader.py's/test_promotion.py's own identically-shaped fixture.
    """
    db_session.execute(update(ModelVersion).values(is_champion=False))
    db_session.commit()


@pytest.fixture(autouse=True)
def _clear_champion_flag(db_session):
    _clear_existing_champion(db_session)
    yield
    _clear_existing_champion(db_session)


@pytest.fixture(autouse=True)
def _cleanup_this_seasons_features(db_session):
    """This module's own HISTORICAL_DATA_INGESTION + FEATURE_GENERATION seeding leaves real
    player_features rows behind for "2023-24" in this shared, non-truncated Postgres -- those leak
    into every OTHER test's GLOBAL usable_season_labels_with_features query (see
    tests/integration/test_start_probability_training_job.py's own identical, established
    precedent for this exact pollution risk)."""
    yield
    season = db_session.execute(select(Season.id).where(Season.label == "2023-24")).scalar_one_or_none()
    if season is not None:
        db_session.execute(delete(PlayerFeatures).where(PlayerFeatures.season_id == season))
        db_session.commit()


def _seed_champion_model_version_id(db_session, patched_historical_fpl_client) -> uuid.UUID:
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
    return uuid.UUID(training_job.output_json["champion"]["model_version_id"])


def test_get_champion_model_happy_path(db_session, patched_historical_fpl_client):
    champion_id = _seed_champion_model_version_id(db_session, patched_historical_fpl_client)
    model_version = db_session.get(ModelVersion, champion_id)
    client = authed_client()

    response = client.get("/models/champion")

    assert response.status_code == 200
    body = response.json()
    assert body["model_version_id"] == str(champion_id)
    assert body["model_family"] == model_version.model_family.value
    assert body["feature_version"] == 1
    assert body["trained_at"]


def test_get_champion_model_none_selected_returns_404(db_session):
    # _clear_champion_flag (autouse) guarantees no is_champion=True row exists
    # anywhere in the table at this point, regardless of what other test
    # modules left behind in this same shared, non-truncated Postgres.
    client = authed_client()

    response = client.get("/models/champion")

    assert response.status_code == 404
    assert response.json()["error_code"] == "NO_CHAMPION_MODEL"


def test_post_model_training_bootstrap_case_promotes_immediately(db_session, patched_historical_fpl_client):
    """No champion exists yet -- mirrors app.jobs.handlers.model_training's own bootstrap-case
    precedent (test_model_training_job.py). Real gap this endpoint closes: MODEL_TRAINING previously
    had no HTTP trigger at all, unlike its sibling START_PROBABILITY_MODEL_TRAINING -- reachable only
    via CLI or train.yml (a throwaway CI database, never this app's own real live deployment)."""
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
    client = authed_client()

    response = client.post("/models/train", json={})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["usable_seasons"] == ["2023-24"]
    assert body["promotion_outcome"] == "champion"
    assert body["lifecycle_state"] == "champion"

    champion = db_session.get(ModelVersion, uuid.UUID(body["champion_model_version_id"]))
    assert champion is not None
    assert champion.is_champion is True


def test_post_model_training_error_response(db_session):
    client = authed_client()

    response = client.post("/models/train", json={})

    assert response.status_code == 409
    assert response.json()["error_code"] == "NO_SEASONS_WITH_FEATURES"
