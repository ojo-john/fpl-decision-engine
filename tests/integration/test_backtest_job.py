"""Integration tests for the BACKTEST job (Milestone 7 Layer 6) end-to-end through execute_job.

Requires a running Postgres with migrations applied (see conftest.py). Reuses
``tests/integration/test_model_training_job.py``'s own committed-CSV-fixture
seeding pattern (duplicated, not imported -- this repo's own established
"duplicate test setup rather than cross-import between test modules"
convention, see e.g. ``test_api_learning.py``'s own module docstring) to get
a REAL, already-trained ``model_versions`` row via a genuine MODEL_TRAINING
run, then backtests it -- proving BACKTEST reuses ``app.modeling
.walk_forward``'s own fold-building against the CURRENT dataset without
refitting/retraining the model itself, and persists its own, clearly
distinguishable ``backtest_runs`` row (never touching ``model_metrics``, the
original training run's own audit trail).
"""

import uuid
from datetime import UTC, datetime
from pathlib import Path

import joblib
from sqlalchemy import select

from app.jobs.service import execute_job
from app.modeling import candidates as candidate_models
from packages.database.models.backtest_run import BacktestRun
from packages.database.models.model_metric import ModelMetric
from packages.database.models.model_version import ModelVersion
from packages.database.models.player_features import PlayerFeatures
from packages.database.models.season import Season
from packages.domain.jobs import JobStatus, JobType, TriggerType
from packages.domain.learning import BacktestRunStatus
from packages.domain.modeling import ModelFamily


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
    return db_session.execute(select(Season).where(Season.label == "2023-24")).scalar_one()


def _run_model_training_job(db_session, feature_version: int = 1):
    job = execute_job(
        db_session,
        job_type=JobType.MODEL_TRAINING,
        parameters={"feature_version": feature_version},
        trigger_type=TriggerType.LOCAL,
    )
    assert job.status == JobStatus.SUCCEEDED
    return job


def _seed_trained_model_version(db_session, family: ModelFamily) -> uuid.UUID:
    """Seed one real, already-trained ``model_versions`` row for ``family`` via a genuine
    MODEL_TRAINING run against the committed ``2023-24`` historical fixture.
    """
    _seed_historical_season(db_session)
    training_job = _run_model_training_job(db_session)
    version = db_session.execute(
        select(ModelVersion).where(
            ModelVersion.training_job_id == training_job.id, ModelVersion.model_family == family
        )
    ).scalar_one()
    return version.id


def _seed_manual_tree_model_version(db_session, tmp_path, season_label: str = "2023-24") -> uuid.UUID:
    """Fit and persist a real, already-trained ``DecisionTreeRegressor`` artifact directly -- NOT via
    a full MODEL_TRAINING run, since that run's own champion selection isn't guaranteed to pick a
    tree family for this particular fixture dataset (only the overall WINNER of a training run ever
    gets an artifact -- see ``app.jobs.handlers.model_training``'s own module docstring).

    This gives deterministic coverage of BACKTEST's own tree-family prediction path
    (``build_feature_matrix``/``align_columns``/a joblib-loaded ``sklearn_model.predict``),
    decoupled from which family a real walk-forward run happens to prefer.
    """
    season = db_session.execute(select(Season).where(Season.label == season_label)).scalar_one()
    rows = list(
        db_session.execute(
            select(PlayerFeatures).where(
                PlayerFeatures.season_id == season.id, PlayerFeatures.feature_version == 1
            )
        )
        .scalars()
        .all()
    )
    x, y = candidate_models.build_feature_matrix(rows)
    x = x[candidate_models.select_non_degenerate_columns(x)]
    model = candidate_models.build_decision_tree()
    model.fit(x, y)

    artifact_path = tmp_path / "backtest_test_model.joblib"
    joblib.dump(model, artifact_path)

    version = ModelVersion(
        model_name="backtest-test-tree",
        model_family=ModelFamily.DECISION_TREE,
        feature_version=1,
        target_variable="total_points",
        training_seasons_json=[season_label],
        artifact_uri=str(artifact_path),
        # The sole source BACKTEST actually loads from -- see ModelVersion.artifact_uri's own
        # docstring on why a local filesystem path alone is never authoritative.
        artifact_bytes=artifact_path.read_bytes(),
        trained_at=datetime.now(UTC),
    )
    db_session.add(version)
    db_session.commit()
    db_session.refresh(version)
    return version.id


def _run_backtest_job(db_session, model_version_id):
    return execute_job(
        db_session,
        job_type=JobType.BACKTEST,
        parameters={"model_version_id": str(model_version_id)},
        trigger_type=TriggerType.LOCAL,
    )


# --- Real walk-forward scoring, both a baseline and a tree family ----------------------------------


def test_backtest_produces_real_metrics_for_a_baseline_family_model(
    db_session, patched_historical_fpl_client
):
    model_version_id = _seed_trained_model_version(db_session, ModelFamily.BASELINE_SEASON_AVERAGE)

    job = _run_backtest_job(db_session, model_version_id)

    assert job.status == JobStatus.SUCCEEDED, job.error_message
    output = job.output_json
    assert output["model_version_id"] == str(model_version_id)
    assert output["model_family"] == ModelFamily.BASELINE_SEASON_AVERAGE.value
    assert output["usable_seasons"] == ["2023-24"]
    assert output["fold_count"] == 1
    assert output["degenerate_single_season"] is True
    assert output["aggregate_mae"] >= 0
    assert output["n_observations"] > 0

    row = db_session.get(BacktestRun, uuid.UUID(output["backtest_run_id"]))
    assert row is not None
    assert row.run_status == BacktestRunStatus.SUCCEEDED
    assert row.model_version_id == model_version_id
    assert row.feature_version == 1
    assert row.fold_count == 1
    assert row.degenerate_single_season is True
    assert len(row.fold_metrics_json) == 1
    fold_metric = row.fold_metrics_json[0]
    assert fold_metric["validation_season_label"] == "2023-24"
    assert fold_metric["mae"] >= 0
    assert fold_metric["n_observations"] > 0
    assert row.aggregate_mae is not None and row.aggregate_mae >= 0
    assert row.aggregate_rmse is not None and row.aggregate_rmse >= 0
    assert row.n_observations > 0
    assert row.started_at is not None
    assert row.completed_at is not None


def test_backtest_produces_real_metrics_for_a_tree_family_model(
    db_session, patched_historical_fpl_client, tmp_path
):
    _seed_historical_season(db_session)
    model_version_id = _seed_manual_tree_model_version(db_session, tmp_path)
    model_version = db_session.get(ModelVersion, model_version_id)
    assert model_version.artifact_uri is not None
    assert Path(model_version.artifact_uri).is_file()

    job = _run_backtest_job(db_session, model_version_id)

    assert job.status == JobStatus.SUCCEEDED, job.error_message
    output = job.output_json
    assert output["model_family"] == ModelFamily.DECISION_TREE.value
    assert output["aggregate_mae"] >= 0

    row = db_session.get(BacktestRun, uuid.UUID(output["backtest_run_id"]))
    assert row is not None
    assert row.fold_metrics_json[0]["mae"] >= 0
    assert row.fold_metrics_json[0]["n_observations"] > 0

    # The artifact itself is untouched (never refit) -- still the same file, still loadable.
    assert Path(model_version.artifact_uri).is_file()


# --- Missing/corrupt artifact_bytes fails cleanly, never a raw unhandled exception -----------------
# Real production incident (2026-08-24): a tree-family champion's artifact was lost after a Render
# redeploy wiped its local-disk-only artifact_uri, and POST /jobs/backtest surfaced a raw
# FileNotFoundError to a real user instead of a clean job failure. artifact_bytes (this table's own
# durable, DB-stored column -- see ModelVersion's own docstring) is what this class of failure now
# looks like: still possible (a row from before this column existed, or corrupted bytes), but always
# a clean JobResult.FAILED, never an unhandled exception.


def test_backtest_fails_cleanly_when_artifact_bytes_is_missing(db_session, patched_historical_fpl_client):
    _seed_historical_season(db_session)
    version = ModelVersion(
        model_name="backtest-test-tree-no-bytes",
        model_family=ModelFamily.DECISION_TREE,
        feature_version=1,
        target_variable="total_points",
        training_seasons_json=["2023-24"],
        artifact_uri="models/some_old_pre_fix_row/model.joblib",
        artifact_bytes=None,
        trained_at=datetime.now(UTC),
    )
    db_session.add(version)
    db_session.commit()
    db_session.refresh(version)

    job = _run_backtest_job(db_session, version.id)

    assert job.status == JobStatus.FAILED
    assert job.error_code == "MODEL_ARTIFACT_MISSING"


def test_backtest_fails_cleanly_when_artifact_bytes_is_corrupt(db_session, patched_historical_fpl_client):
    _seed_historical_season(db_session)
    version = ModelVersion(
        model_name="backtest-test-tree-corrupt-bytes",
        model_family=ModelFamily.DECISION_TREE,
        feature_version=1,
        target_variable="total_points",
        training_seasons_json=["2023-24"],
        artifact_uri="models/corrupt/model.joblib",
        artifact_bytes=b"not a real joblib artifact",
        trained_at=datetime.now(UTC),
    )
    db_session.add(version)
    db_session.commit()
    db_session.refresh(version)

    job = _run_backtest_job(db_session, version.id)

    assert job.status == JobStatus.FAILED
    assert job.error_code == "MODEL_ARTIFACT_LOAD_FAILED"


# --- Never retrains, never mutates the original training run's own audit trail ---------------------


def test_backtest_does_not_create_a_new_model_version_row(
    db_session, patched_historical_fpl_client, tmp_path
):
    _seed_historical_season(db_session)
    model_version_id = _seed_manual_tree_model_version(db_session, tmp_path)
    version_count_before = len(db_session.execute(select(ModelVersion)).scalars().all())

    job = _run_backtest_job(db_session, model_version_id)
    assert job.status == JobStatus.SUCCEEDED, job.error_message

    version_count_after = len(db_session.execute(select(ModelVersion)).scalars().all())
    assert version_count_after == version_count_before

    # The original model_versions row itself is completely untouched.
    version = db_session.get(ModelVersion, model_version_id)
    assert version.lifecycle_state.value == "candidate"
    assert version.is_champion is False


def test_backtest_does_not_mutate_the_original_training_runs_model_metrics_rows(
    db_session, patched_historical_fpl_client
):
    model_version_id = _seed_trained_model_version(db_session, ModelFamily.BASELINE_SEASON_AVERAGE)
    original_metrics = (
        db_session.execute(select(ModelMetric).where(ModelMetric.model_version_id == model_version_id))
        .scalars()
        .all()
    )
    original_metrics_snapshot = {
        (m.id, m.fold_index): (m.mae, m.rmse, m.n_observations) for m in original_metrics
    }
    assert original_metrics_snapshot  # sanity: the original training run really did leave metrics behind

    job = _run_backtest_job(db_session, model_version_id)
    assert job.status == JobStatus.SUCCEEDED, job.error_message

    metrics_after = (
        db_session.execute(select(ModelMetric).where(ModelMetric.model_version_id == model_version_id))
        .scalars()
        .all()
    )
    metrics_after_snapshot = {(m.id, m.fold_index): (m.mae, m.rmse, m.n_observations) for m in metrics_after}
    # Exactly the same rows, byte-for-byte -- BACKTEST's own output lives only in backtest_runs.
    assert metrics_after_snapshot == original_metrics_snapshot


# --- Rerun behavior: insert-only, each run its own recordable event --------------------------------


def test_backtest_rerun_creates_a_second_insert_only_row(db_session, patched_historical_fpl_client):
    model_version_id = _seed_trained_model_version(db_session, ModelFamily.BASELINE_SEASON_AVERAGE)

    first_job = _run_backtest_job(db_session, model_version_id)
    assert first_job.status == JobStatus.SUCCEEDED
    second_job = _run_backtest_job(db_session, model_version_id)
    assert second_job.status == JobStatus.SUCCEEDED

    assert first_job.output_json["backtest_run_id"] != second_job.output_json["backtest_run_id"]
    rows = (
        db_session.execute(select(BacktestRun).where(BacktestRun.model_version_id == model_version_id))
        .scalars()
        .all()
    )
    assert len(rows) == 2


# --- Refusal paths ----------------------------------------------------------------------------------


def test_backtest_model_version_not_found_returns_failed_no_row(db_session):
    unknown_id = uuid.uuid4()

    job = _run_backtest_job(db_session, unknown_id)

    assert job.status == JobStatus.FAILED
    assert job.error_code == "MODEL_VERSION_NOT_FOUND"
    rows = (
        db_session.execute(select(BacktestRun).where(BacktestRun.model_version_id == unknown_id))
        .scalars()
        .all()
    )
    assert rows == []


def test_backtest_missing_model_version_id_parameter_returns_failed(db_session):
    job = execute_job(db_session, job_type=JobType.BACKTEST, parameters={}, trigger_type=TriggerType.LOCAL)

    assert job.status == JobStatus.FAILED
    assert job.error_code == "INVALID_MODEL_VERSION_ID"
