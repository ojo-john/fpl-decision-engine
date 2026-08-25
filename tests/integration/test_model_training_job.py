"""Integration tests for the MODEL_TRAINING job end-to-end through execute_job.

Requires a running Postgres with migrations applied (see conftest.py). Seeds
real data via HISTORICAL_DATA_INGESTION + FEATURE_GENERATION against the
committed ``tests/fixtures/historical/2023-24/`` CSV fixture (the same
fixture Layers 1-2's own tests use — ``patched_historical_fpl_client`` in
conftest.py). This gives exactly ONE usable season, which per
``app.modeling.walk_forward``'s design should gracefully degrade to a single
in-sample fold rather than crash -- this is asserted explicitly below, since
it is the realistic state of a freshly-cloned dev environment before a full
multi-season backfill has been run. No real network access happens in this
module.
"""

import io
from pathlib import Path

import joblib
from sqlalchemy import select, update

from app.jobs.service import execute_job
from packages.database.models.model_metric import ModelMetric
from packages.database.models.model_version import ModelVersion
from packages.database.models.season import Season
from packages.domain.jobs import JobStatus, JobType, TriggerType
from packages.domain.learning import ModelLifecycleState
from packages.domain.modeling import ModelFamily

_ALL_SEVEN_FAMILIES = {family.value for family in ModelFamily}


def _clear_existing_champion(db_session) -> None:
    """Force a deterministic "no champion exists yet" starting state for a Layer 5 bootstrap-case
    test -- mirrors ``tests/integration/test_champion_loader.py``'s own ``_clear_existing_champion``.
    This module's OTHER, pre-existing tests never needed this (they only assert "exactly one
    champion exists", true regardless of which run created it), but Layer 5's bootstrap-vs-not
    distinction genuinely depends on whether a champion already exists BEFORE this test's own first
    MODEL_TRAINING call -- which, on this shared, non-truncated-between-tests Postgres, otherwise
    depends on what earlier tests in this same session happened to leave behind.
    """
    db_session.execute(
        update(ModelVersion).values(is_champion=False, lifecycle_state=ModelLifecycleState.CANDIDATE)
    )
    db_session.commit()


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
    return execute_job(
        db_session,
        job_type=JobType.MODEL_TRAINING,
        parameters={"feature_version": feature_version},
        trigger_type=TriggerType.LOCAL,
    )


def test_model_training_single_season_degrades_gracefully(db_session, patched_historical_fpl_client):
    """A single usable season must not crash walk-forward validation -- it degrades to one in-sample fold."""
    _seed_historical_season(db_session)

    job = _run_model_training_job(db_session)

    assert job.status == JobStatus.SUCCEEDED
    output = job.output_json
    assert output["usable_seasons"] == ["2023-24"]
    assert output["fold_count"] == 1
    assert output["degenerate_single_season"] is True


def test_model_training_creates_all_seven_candidates_with_sane_metrics(
    db_session, patched_historical_fpl_client
):
    _seed_historical_season(db_session)
    job = _run_model_training_job(db_session)
    assert job.status == JobStatus.SUCCEEDED

    versions = (
        db_session.execute(select(ModelVersion).where(ModelVersion.training_job_id == job.id)).scalars().all()
    )

    assert {v.model_family.value for v in versions} == _ALL_SEVEN_FAMILIES
    assert len(versions) == 7

    for version in versions:
        assert version.feature_version == 1
        assert version.target_variable == "total_points"
        assert version.training_seasons_json == ["2023-24"]
        assert version.trained_at is not None

        metrics = (
            db_session.execute(select(ModelMetric).where(ModelMetric.model_version_id == version.id))
            .scalars()
            .all()
        )
        # At least the aggregate row must exist for every candidate that
        # scored successfully on at least one fold -- with real fixture
        # data (played gameweeks, real xp_estimate values), every one of
        # the 7 candidates is expected to be scoreable here.
        assert len(metrics) >= 1
        for metric in metrics:
            assert metric.mae >= 0
            assert metric.rmse >= 0
            assert metric.n_observations > 0
            if metric.spearman_rank_corr is not None:
                assert -1.0 <= float(metric.spearman_rank_corr) <= 1.0
            if metric.top_20_capture_rate is not None:
                assert 0.0 <= float(metric.top_20_capture_rate) <= 1.0


def test_model_training_exactly_one_champion_across_whole_table(db_session, patched_historical_fpl_client):
    _seed_historical_season(db_session)
    job = _run_model_training_job(db_session)
    assert job.status == JobStatus.SUCCEEDED

    champions = (
        db_session.execute(select(ModelVersion).where(ModelVersion.is_champion.is_(True))).scalars().all()
    )
    assert len(champions) == 1


def test_model_training_baseline_rules_ep_next_always_gets_a_row(db_session, patched_historical_fpl_client):
    _seed_historical_season(db_session)
    job = _run_model_training_job(db_session)
    assert job.status == JobStatus.SUCCEEDED

    ep_next_version = db_session.execute(
        select(ModelVersion).where(
            ModelVersion.training_job_id == job.id,
            ModelVersion.model_family == ModelFamily.BASELINE_RULES_EP_NEXT,
        )
    ).scalar_one_or_none()
    assert ep_next_version is not None
    assert ep_next_version.artifact_uri is None  # baselines never get a serialized artifact


def test_model_training_champion_artifact_loads_and_predicts_if_tree_family(
    db_session, patched_historical_fpl_client
):
    _seed_historical_season(db_session)
    job = _run_model_training_job(db_session)
    assert job.status == JobStatus.SUCCEEDED

    champion = db_session.execute(select(ModelVersion).where(ModelVersion.is_champion.is_(True))).scalar_one()

    if champion.model_family in (
        ModelFamily.DECISION_TREE,
        ModelFamily.RANDOM_FOREST,
        ModelFamily.GRADIENT_BOOSTING,
    ):
        assert champion.artifact_uri is not None
        assert champion.artifact_checksum_sha256 is not None

        artifact_path = Path(champion.artifact_uri)
        assert artifact_path.is_file()

        loaded_model = joblib.load(artifact_path)
        assert hasattr(loaded_model, "predict")

        # artifact_bytes is the sole authoritative source champion_loader/backtest actually read
        # from (a local filesystem path never survives a real Render redeploy -- see
        # ModelVersion.artifact_uri's own docstring) -- must be independently loadable too.
        assert champion.artifact_bytes is not None
        loaded_from_bytes = joblib.load(io.BytesIO(champion.artifact_bytes))
        assert hasattr(loaded_from_bytes, "predict")
    else:
        assert champion.artifact_uri is None
        assert champion.artifact_bytes is None


def test_model_training_rerun_creates_a_fresh_snapshot_not_an_upsert(
    db_session, patched_historical_fpl_client
):
    """Each MODEL_TRAINING run is its own audit snapshot -- re-running inserts NEW rows, never upserts."""
    _seed_historical_season(db_session)

    first_job = _run_model_training_job(db_session)
    assert first_job.status == JobStatus.SUCCEEDED
    second_job = _run_model_training_job(db_session)
    assert second_job.status == JobStatus.SUCCEEDED

    assert first_job.id != second_job.id

    first_versions = (
        db_session.execute(select(ModelVersion).where(ModelVersion.training_job_id == first_job.id))
        .scalars()
        .all()
    )
    second_versions = (
        db_session.execute(select(ModelVersion).where(ModelVersion.training_job_id == second_job.id))
        .scalars()
        .all()
    )
    assert len(first_versions) == 7
    assert len(second_versions) == 7
    assert {v.id for v in first_versions}.isdisjoint({v.id for v in second_versions})

    # Exactly one champion across the WHOLE table still holds after a second run.
    champions = (
        db_session.execute(select(ModelVersion).where(ModelVersion.is_champion.is_(True))).scalars().all()
    )
    assert len(champions) == 1


def test_model_training_bootstrap_case_flags_winner_champion_with_champion_lifecycle_state(
    db_session, patched_historical_fpl_client
):
    """Milestone 7 Layer 5: the bootstrap case (no existing champion anywhere in the table) is
    UNCHANGED -- the winner is promoted immediately, and additionally gets lifecycle_state=CHAMPION."""
    _clear_existing_champion(db_session)
    _seed_historical_season(db_session)
    job = _run_model_training_job(db_session)
    assert job.status == JobStatus.SUCCEEDED

    champion = db_session.execute(select(ModelVersion).where(ModelVersion.is_champion.is_(True))).scalar_one()
    assert champion.lifecycle_state == ModelLifecycleState.CHAMPION
    assert job.output_json["champion"]["promotion_outcome"] == "champion"
    assert job.output_json["champion"]["lifecycle_state"] == "champion"


def test_model_training_second_run_with_existing_champion_creates_shadow_challenger_not_a_new_champion(
    db_session, patched_historical_fpl_client
):
    """Milestone 7 Layer 5's surgical change: once a champion already exists, a second MODEL_TRAINING
    run's own winner becomes a SHADOW_CHALLENGER, never touching is_champion -- the existing champion
    is completely untouched."""
    _clear_existing_champion(db_session)
    _seed_historical_season(db_session)

    first_job = _run_model_training_job(db_session)
    assert first_job.status == JobStatus.SUCCEEDED
    existing_champion = db_session.execute(
        select(ModelVersion).where(ModelVersion.is_champion.is_(True))
    ).scalar_one()
    existing_champion_id = existing_champion.id
    existing_champion_trained_at = existing_champion.trained_at

    second_job = _run_model_training_job(db_session)
    assert second_job.status == JobStatus.SUCCEEDED

    # The pre-existing champion is completely untouched.
    db_session.refresh(existing_champion)
    assert existing_champion.is_champion is True
    assert existing_champion.lifecycle_state == ModelLifecycleState.CHAMPION
    assert existing_champion.trained_at == existing_champion_trained_at

    # Exactly one champion across the whole table still -- the second run added no new one.
    champions = (
        db_session.execute(select(ModelVersion).where(ModelVersion.is_champion.is_(True))).scalars().all()
    )
    assert len(champions) == 1
    assert champions[0].id == existing_champion_id

    # This second run's own 7 candidates: the winner is a SHADOW_CHALLENGER, is_champion stays False
    # on every one of them.
    second_versions = (
        db_session.execute(select(ModelVersion).where(ModelVersion.training_job_id == second_job.id))
        .scalars()
        .all()
    )
    assert len(second_versions) == 7
    assert all(v.is_champion is False for v in second_versions)

    shadow_challengers = [
        v for v in second_versions if v.lifecycle_state == ModelLifecycleState.SHADOW_CHALLENGER
    ]
    assert len(shadow_challengers) == 1
    assert str(shadow_challengers[0].id) == second_job.output_json["champion"]["model_version_id"]
    assert second_job.output_json["champion"]["promotion_outcome"] == "shadow_challenger"
    assert second_job.output_json["champion"]["lifecycle_state"] == "shadow_challenger"

    # Every other one of the second run's 7 candidates stays a plain CANDIDATE.
    non_winners = [v for v in second_versions if v.id != shadow_challengers[0].id]
    assert all(v.lifecycle_state == ModelLifecycleState.CANDIDATE for v in non_winners)


def test_model_training_shadow_challenger_replaces_prior_shadow_challenger_lifecycle_but_not_champion(
    db_session, patched_historical_fpl_client
):
    """A THIRD run, with a champion still in place and an existing (not-yet-decided) shadow
    challenger from the previous run, does not disturb the champion or the prior challenger's own
    lifecycle_state -- it just adds its own fresh candidate rows, one of which becomes ANOTHER shadow
    challenger. (No "at most one shadow challenger" DB constraint is enforced -- see
    app.optimization.champion_loader.load_shadow_challenger's own docstring for how the most-recent
    one is resolved when this happens.)"""
    _seed_historical_season(db_session)

    first_job = _run_model_training_job(db_session)
    assert first_job.status == JobStatus.SUCCEEDED
    second_job = _run_model_training_job(db_session)
    assert second_job.status == JobStatus.SUCCEEDED
    prior_shadow_challenger_id = second_job.output_json["champion"]["model_version_id"]

    third_job = _run_model_training_job(db_session)
    assert third_job.status == JobStatus.SUCCEEDED

    prior_shadow_challenger = db_session.get(ModelVersion, prior_shadow_challenger_id)
    assert prior_shadow_challenger.lifecycle_state == ModelLifecycleState.SHADOW_CHALLENGER
    assert prior_shadow_challenger.is_champion is False

    champions = (
        db_session.execute(select(ModelVersion).where(ModelVersion.is_champion.is_(True))).scalars().all()
    )
    assert len(champions) == 1

    third_versions = (
        db_session.execute(select(ModelVersion).where(ModelVersion.training_job_id == third_job.id))
        .scalars()
        .all()
    )
    shadow_challengers = [
        v for v in third_versions if v.lifecycle_state == ModelLifecycleState.SHADOW_CHALLENGER
    ]
    assert len(shadow_challengers) == 1
