"""Integration tests for the POSITION_SEGMENTATION_VALIDATION job end-to-end through execute_job.

Requires a running Postgres with migrations applied (see conftest.py). Reuses
``tests/integration/test_backtest_job.py``'s own committed-CSV-fixture seeding pattern
(duplicated, not imported -- this repo's own established "duplicate test setup rather than
cross-import between test modules" convention) to get REAL ``player_features`` rows via a genuine
``HISTORICAL_DATA_INGESTION`` + ``FEATURE_GENERATION`` run against the committed ``2023-24``
fixture, then runs POSITION_SEGMENTATION_VALIDATION against it -- proving the job produces a real,
per-position MAE comparison and persists its own, clearly distinguishable
``position_segmentation_validation_runs`` row, never touching ``model_versions``/``is_champion``.
"""

import uuid

from sqlalchemy import select

from app.jobs.service import execute_job
from packages.database.models.model_version import ModelVersion
from packages.database.models.position_segmentation_validation_run import PositionSegmentationValidationRun
from packages.database.models.season import Season
from packages.domain.jobs import JobStatus, JobType, TriggerType
from packages.domain.learning import PositionSegmentationValidationRunStatus


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


def _run_position_segmentation_validation_job(db_session, feature_version: int = 1):
    return execute_job(
        db_session,
        job_type=JobType.POSITION_SEGMENTATION_VALIDATION,
        parameters={"feature_version": feature_version},
        trigger_type=TriggerType.LOCAL,
    )


def test_position_segmentation_validation_produces_real_per_position_results_end_to_end(
    db_session, patched_historical_fpl_client
):
    _seed_historical_season(db_session)

    job = _run_position_segmentation_validation_job(db_session)

    assert job.status == JobStatus.SUCCEEDED, job.error_message
    output = job.output_json
    assert output["feature_version"] == 1
    assert output["usable_seasons"] == ["2023-24"]
    assert output["fold_count"] == 1
    assert output["degenerate_single_season"] is True
    assert output["n_observations"] > 0
    assert set(output["per_position"].keys()) == {"GKP", "DEF", "MID", "FWD"}
    for position_values in output["per_position"].values():
        # A real committed fixture -- every position should have real observations and a real MAE.
        if position_values["n_observations"] > 0:
            assert position_values["global_mae"] is not None
            assert position_values["global_mae"] >= 0

    row = db_session.get(
        PositionSegmentationValidationRun, uuid.UUID(output["position_segmentation_validation_run_id"])
    )
    assert row is not None
    assert row.run_status == PositionSegmentationValidationRunStatus.SUCCEEDED
    assert row.feature_version == 1
    assert row.fold_count == 1
    assert row.degenerate_single_season is True
    assert row.n_observations > 0
    assert row.started_at is not None
    assert row.completed_at is not None
    assert set(row.per_position_json.keys()) == {"GKP", "DEF", "MID", "FWD"}


def test_position_segmentation_validation_never_creates_a_model_version_or_touches_champion_state(
    db_session, patched_historical_fpl_client
):
    _seed_historical_season(db_session)
    version_count_before = len(db_session.execute(select(ModelVersion)).scalars().all())

    job = _run_position_segmentation_validation_job(db_session)

    assert job.status == JobStatus.SUCCEEDED, job.error_message
    version_count_after = len(db_session.execute(select(ModelVersion)).scalars().all())
    assert version_count_after == version_count_before


def test_position_segmentation_validation_rerun_creates_a_second_insert_only_row(
    db_session, patched_historical_fpl_client
):
    """Asserts a DELTA (count before -> count after), not an absolute count -- this project's
    integration tests run against a shared, never-truncated Postgres (see conftest.py's module
    docstring), and feature_version=1 is not unique to this test, so an earlier test run (in this
    same file, or a prior full-suite run against the same volume) may have already left rows
    behind. Mirrors this exact class of fix already applied elsewhere in this codebase (e.g.
    tests/integration/test_optimization_uses_champion.py's own per-player cold-start test)."""
    _seed_historical_season(db_session)
    count_before = len(
        db_session.execute(
            select(PositionSegmentationValidationRun).where(
                PositionSegmentationValidationRun.feature_version == 1
            )
        )
        .scalars()
        .all()
    )

    first_job = _run_position_segmentation_validation_job(db_session)
    assert first_job.status == JobStatus.SUCCEEDED
    second_job = _run_position_segmentation_validation_job(db_session)
    assert second_job.status == JobStatus.SUCCEEDED

    first_run_id = first_job.output_json["position_segmentation_validation_run_id"]
    second_run_id = second_job.output_json["position_segmentation_validation_run_id"]
    assert first_run_id != second_run_id

    rows = (
        db_session.execute(
            select(PositionSegmentationValidationRun).where(
                PositionSegmentationValidationRun.feature_version == 1
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == count_before + 2
    row_ids = {str(row.id) for row in rows}
    assert first_run_id in row_ids
    assert second_run_id in row_ids


def test_position_segmentation_validation_no_seasons_with_features_returns_failed(db_session):
    job = _run_position_segmentation_validation_job(db_session, feature_version=999)

    assert job.status == JobStatus.FAILED
    assert job.error_code == "NO_SEASONS_WITH_FEATURES"
    rows = (
        db_session.execute(
            select(PositionSegmentationValidationRun).where(
                PositionSegmentationValidationRun.feature_version == 999
            )
        )
        .scalars()
        .all()
    )
    assert rows == []
