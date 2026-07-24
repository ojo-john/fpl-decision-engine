"""Integration tests for GET /models/champion against a real database.

Mirrors test_api_learning.py's own _seed_trained_model_version_id helper
(duplicated, not imported -- this repo's established "duplicate test setup
across HTTP-layer test modules" convention) to get a real champion
model_versions row via a genuine HISTORICAL_DATA_INGESTION -> FEATURE_GENERATION
-> MODEL_TRAINING pipeline.
"""

import uuid

import pytest
from sqlalchemy import update

from app.jobs.service import execute_job
from packages.database.models.model_version import ModelVersion
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
