"""Integration tests for START_PROBABILITY_MODEL_TRAINING end-to-end through execute_job, plus the
real, DB-backed proof that an activated row actually adjusts a live projection.

Reuses tests/integration/test_backtest_job.py's/test_optimization_uses_champion.py's own
committed-CSV-fixture seeding pattern (duplicated, not imported -- this repo's own established
"duplicate test setup rather than cross-import between test modules" convention).
"""

import io
import uuid

import joblib
import pytest
from sqlalchemy import delete, select

from app.jobs.service import execute_job
from app.optimization.projection import START_PROBABILITY_ADJUSTED_SUFFIX, project_player_with_champion
from app.optimization.start_probability_loader import load_active_start_probability_model
from packages.database.models.gameweek import Gameweek
from packages.database.models.player import Player
from packages.database.models.player_features import PlayerFeatures
from packages.database.models.season import Season
from packages.database.models.start_probability_model import StartProbabilityModel
from packages.domain.jobs import JobStatus, JobType, TriggerType
from tests.integration.conftest import authed_client


def _seed_historical_season(db_session, season_label: str = "2023-24"):
    ingestion_job = execute_job(
        db_session,
        job_type=JobType.HISTORICAL_DATA_INGESTION,
        parameters={"season_label": season_label},
        trigger_type=TriggerType.LOCAL,
    )
    assert ingestion_job.status == JobStatus.SUCCEEDED

    features_job = execute_job(
        db_session,
        job_type=JobType.FEATURE_GENERATION,
        parameters={"season_label": season_label},
        trigger_type=TriggerType.LOCAL,
    )
    assert features_job.status == JobStatus.SUCCEEDED


def _run_start_probability_training(db_session):
    return execute_job(
        db_session,
        job_type=JobType.START_PROBABILITY_MODEL_TRAINING,
        trigger_type=TriggerType.LOCAL,
    )


@pytest.fixture(autouse=True)
def _cleanup_this_seasons_features_and_models(db_session):
    """This module's own HISTORICAL_DATA_INGESTION + FEATURE_GENERATION seeding leaves real
    player_features rows behind for "2023-24" in this shared, non-truncated Postgres -- those
    leak into every OTHER test module's GLOBAL usable_season_labels_with_features query (see
    tests/integration/test_optimization_uses_champion.py's own module docstring for the exact,
    real cross-test-pollution bug this prevents). Deletes this module's own start_probability_models
    rows too, so is_active defaults are never accidentally left True for a later test/module.
    """
    yield
    season = db_session.execute(select(Season.id).where(Season.label == "2023-24")).scalar_one_or_none()
    if season is not None:
        db_session.execute(delete(PlayerFeatures).where(PlayerFeatures.season_id == season))
    db_session.execute(delete(StartProbabilityModel))
    db_session.commit()


# --- Real walk-forward scoring, real persistence -------------------------------------------------


def test_start_probability_training_produces_real_metrics_and_stays_inactive(
    db_session, patched_historical_fpl_client
):
    _seed_historical_season(db_session)

    job = _run_start_probability_training(db_session)

    assert job.status == JobStatus.SUCCEEDED, job.error_message
    output = job.output_json
    assert output["usable_seasons"] == ["2023-24"]
    assert output["fold_count"] == 1
    assert output["degenerate_single_season"] is True
    assert output["n_observations"] > 0
    assert output["is_active"] is False

    row = db_session.get(StartProbabilityModel, uuid.UUID(output["start_probability_model_id"]))
    assert row is not None
    assert row.is_active is False  # the mandatory default -- see this table's own docstring
    assert row.target_variable == "started_minutes_ge_60"
    assert len(row.fold_metrics_json) == 1
    assert row.n_observations > 0
    assert row.artifact_uri is not None

    # Real artifact -- proves the final refit actually happened and produces a usable classifier.
    model = joblib.load(row.artifact_uri)
    assert hasattr(model, "predict_proba")

    # artifact_bytes is the sole authoritative source start_probability_loader actually reads
    # from (a local filesystem path never survives a real Render redeploy -- see
    # ModelVersion.artifact_uri's own docstring) -- must be independently loadable too.
    assert row.artifact_bytes is not None
    model_from_bytes = joblib.load(io.BytesIO(row.artifact_bytes))
    assert hasattr(model_from_bytes, "predict_proba")


def test_start_probability_training_fails_cleanly_with_no_features(db_session):
    job = _run_start_probability_training(db_session)

    assert job.status == JobStatus.FAILED
    assert job.error_code == "NO_SEASONS_WITH_FEATURES"
    assert db_session.execute(select(StartProbabilityModel)).first() is None


# --- HTTP layer --------------------------------------------------------------------------------


def test_post_start_probability_training_endpoint_happy_path(db_session, patched_historical_fpl_client):
    _seed_historical_season(db_session)
    client = authed_client()

    response = client.post("/models/start-probability-training", json={})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["is_active"] is False
    assert body["usable_seasons"] == ["2023-24"]
    assert body["fold_count"] == 1


def test_post_start_probability_training_endpoint_error_response(db_session):
    client = authed_client()

    response = client.post("/models/start-probability-training", json={})

    assert response.status_code == 409
    assert response.json()["error_code"] == "NO_SEASONS_WITH_FEATURES"


def test_get_start_probability_model_by_id_and_list(db_session, patched_historical_fpl_client):
    _seed_historical_season(db_session)
    job = _run_start_probability_training(db_session)
    row_id = job.output_json["start_probability_model_id"]
    client = authed_client()

    get_response = client.get(f"/models/start-probability/{row_id}")
    assert get_response.status_code == 200
    assert get_response.json()["start_probability_model_id"] == row_id

    list_response = client.get("/models/start-probability")
    assert list_response.status_code == 200
    assert any(item["start_probability_model_id"] == row_id for item in list_response.json())


def test_get_start_probability_model_not_found_returns_404(db_session):
    client = authed_client()

    response = client.get(f"/models/start-probability/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["error_code"] == "START_PROBABILITY_MODEL_NOT_FOUND"


# --- Real, DB-backed proof that activation actually changes a live projection --------------------


def test_load_active_start_probability_model_returns_none_by_default(
    db_session, patched_historical_fpl_client
):
    _seed_historical_season(db_session)
    _run_start_probability_training(db_session)

    loaded = load_active_start_probability_model(db_session)

    assert loaded is None  # the mandatory, safe default -- see start_probability_loader's own docstring


def test_activated_start_probability_model_scales_a_real_live_projection_end_to_end(
    db_session, patched_historical_fpl_client
):
    """The real, end-to-end proof this whole mechanism works: manually flip is_active (simulating
    the deliberate human decision this codebase requires -- see StartProbabilityModel's own
    docstring), load it for real, and confirm project_player_with_champion's output actually
    differs from the same call without it -- not just a mocked/faked unit-level assertion."""
    _seed_historical_season(db_session)
    job = _run_start_probability_training(db_session)
    row = db_session.get(StartProbabilityModel, uuid.UUID(job.output_json["start_probability_model_id"]))
    row.is_active = True
    db_session.commit()

    season = db_session.execute(select(Season).where(Season.label == "2023-24")).scalar_one()
    # A gameweek late in the season -- gives real prior-history rows to feed real (non-uniformly-NaN)
    # features, a more meaningful plumbing check than gameweek 1's guaranteed-cold-start shape.
    gameweek = db_session.execute(
        select(Gameweek)
        .where(Gameweek.season_id == season.id)
        .order_by(Gameweek.fpl_event_id.desc())
        .limit(1)
    ).scalar_one()
    feature_row = (
        db_session.execute(
            select(PlayerFeatures).where(
                PlayerFeatures.season_id == season.id,
                PlayerFeatures.gameweek_id == gameweek.id,
                PlayerFeatures.rolling_avg_minutes_3gw.is_not(None),
            )
        )
        .scalars()
        .first()
    )
    assert feature_row is not None, "fixture must have at least one player with real prior minutes history"
    player = db_session.get(Player, feature_row.player_id)

    loaded_model = load_active_start_probability_model(db_session)
    assert loaded_model is not None

    without_adjustment = project_player_with_champion(db_session, player, gameweek.id, champion=None)
    with_adjustment = project_player_with_champion(
        db_session, player, gameweek.id, champion=None, start_probability_model=loaded_model
    )

    assert with_adjustment.source == f"{without_adjustment.source}{START_PROBABILITY_ADJUSTED_SUFFIX}"
    if without_adjustment.projected_points != 0.0:
        assert with_adjustment.projected_points != without_adjustment.projected_points
