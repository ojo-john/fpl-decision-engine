"""Integration tests for the FULL_PIPELINE job end-to-end through execute_job.

Requires a running Postgres with migrations applied (see conftest.py). The
happy-path test first seeds ONE usable historical season (HISTORICAL_DATA_INGESTION
+ FEATURE_GENERATION against the committed ``tests/fixtures/historical/2023-24/``
fixture, the same precedent ``test_model_training_job.py`` itself uses) so
FULL_PIPELINE's own MODEL_TRAINING step has real signal to train against
regardless of what its own DATA_INGESTION/FEATURE_GENERATION steps produce for
the live-mocked "current" season -- mirrors the realistic situation this job
type actually exists for (a routine refresh against an environment that
already has historical data backfilled once). No real network access happens
in this module.
"""

from sqlalchemy import select

from app.jobs.service import execute_job
from packages.database.models.model_version import ModelVersion
from packages.database.models.player_features import PlayerFeatures
from packages.database.models.season import Season
from packages.domain.jobs import JobStatus, JobType, TriggerType


def _seed_historical_season(db_session):
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


def _run_full_pipeline_job(db_session):
    return execute_job(db_session, job_type=JobType.FULL_PIPELINE, trigger_type=TriggerType.LOCAL)


def test_full_pipeline_happy_path_runs_all_three_steps(
    db_session, patched_historical_fpl_client, patched_fpl_client_optimization
):
    _seed_historical_season(db_session)

    job = _run_full_pipeline_job(db_session)

    assert job.status == JobStatus.SUCCEEDED
    steps = job.output_json["steps"]
    assert steps["data_ingestion"]["status"] == "succeeded"
    assert steps["feature_generation"]["status"] == "succeeded"
    assert steps["model_training"]["status"] == "succeeded"

    season_label = job.output_json["season_label"]
    assert season_label == steps["data_ingestion"]["season_label"]

    # feature_generation really targeted the live-mocked "current" season, not "all"/a stale one.
    season_id = db_session.execute(select(Season.id).where(Season.label == season_label)).scalar_one()
    feature_rows = db_session.execute(
        select(PlayerFeatures.id).where(PlayerFeatures.season_id == season_id)
    ).all()
    assert len(feature_rows) > 0

    # model_training really ran and picked a winner -- exactly one champion exists.
    champions = db_session.execute(
        select(ModelVersion.id).where(ModelVersion.is_champion.is_(True))
    ).all()
    assert len(champions) == 1


def test_full_pipeline_stops_at_the_first_failing_step(
    db_session, monkeypatch, patched_historical_fpl_client, patched_fpl_client_optimization
):
    """Force FEATURE_GENERATION to fail -- confirms FULL_PIPELINE reports data_ingestion as
    already-succeeded, feature_generation as the failing step, and never attempts model_training
    against data that never got refreshed."""
    _seed_historical_season(db_session)

    def _boom(*args, **kwargs):
        raise ValueError("simulated feature-computation failure")

    monkeypatch.setattr(
        "app.jobs.handlers.feature_generation.generate_features_for_season", _boom
    )

    job = _run_full_pipeline_job(db_session)

    assert job.status == JobStatus.FAILED
    assert job.error_code == "FEATURE_GENERATION_STEP_FAILED"
    steps = job.output_json["steps"]
    assert steps["data_ingestion"]["status"] == "succeeded"
    assert steps["feature_generation"]["status"] == "failed"
    assert "model_training" not in steps
