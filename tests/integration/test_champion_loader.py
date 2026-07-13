"""Integration tests for the live-usability-aware substitution in ``load_current_champion``.

Milestone 3 Layer 4 wired a trained champion into the live optimizer, but a
capstone live smoke check found that MODEL_TRAINING's champion selection
(honestly, correctly) picks purely on historical walk-forward accuracy --
which can and does elect ``BASELINE_RULES_EP_NEXT``, a family that reads
``player_gameweek_stats.xp_estimate`` for the *current* gameweek and can
therefore NEVER produce a live prediction (live ``DATA_INGESTION`` never
populates that table -- see ``packages.domain.modeling.LIVE_INCAPABLE_MODEL_FAMILIES``).
Without this substitution, the live optimizer would silently get zero
benefit from an entire successful training run, 100% of the time, whenever
that family happens to win.

These tests seed ``model_versions``/``model_metrics`` rows directly (not via
a full MODEL_TRAINING run) so the exact scenario -- a live-incapable family
flagged champion, with a live-capable sibling from the same training run --
is deterministic and doesn't depend on which family a real walk-forward
fold happens to prefer.

This project's integration tests run against a shared, persistent local
Postgres (see conftest.py's module docstring) -- normally
``app.modeling.champion.set_exactly_one_champion`` is what enforces "exactly
one ``is_champion=True`` row across the whole table", but these tests seed
``model_versions`` rows directly, bypassing that invariant-enforcement code
entirely, and (unlike a real MODEL_TRAINING run) the seeded RANDOM_FOREST/
DECISION_TREE rows have no real artifact_uri -- so they must never be left
flagged as champion once this module's tests finish, or a later test module
(e.g. test_api_optimization.py) could pick up this fake, artifact-less
"champion" and misbehave. Every test here clears any pre-existing
``is_champion`` flag BEFORE running (exactly like
``test_optimization_uses_champion.py`` already does), and the module-scoped
autouse fixture below clears it again AFTER every test, regardless of
whether the test passed, failed, or which order tests ran in.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import update

from app.optimization.champion_loader import load_current_champion
from packages.database.models.job import Job
from packages.database.models.model_metric import ModelMetric
from packages.database.models.model_version import ModelVersion
from packages.domain.jobs import JobStatus, JobType, TriggerType
from packages.domain.modeling import ModelFamily


def _clear_existing_champion(db_session) -> None:
    db_session.execute(update(ModelVersion).values(is_champion=False))
    db_session.commit()


@pytest.fixture(autouse=True)
def _clear_champion_flag_after_test(db_session):
    yield
    _clear_existing_champion(db_session)


def _seed_job(db_session) -> uuid.UUID:
    job = Job(
        job_type=JobType.MODEL_TRAINING,
        trigger_type=TriggerType.LOCAL,
        status=JobStatus.SUCCEEDED,
    )
    db_session.add(job)
    db_session.flush()
    return job.id


def _seed_candidate(
    db_session, *, training_job_id: uuid.UUID, family: ModelFamily, mae: str, is_champion: bool
) -> ModelVersion:
    version = ModelVersion(
        model_name=family.value,
        model_family=family,
        feature_version=1,
        target_variable="total_points",
        training_seasons_json=["2023-24", "2024-25"],
        artifact_uri=None,
        is_champion=is_champion,
        trained_at=datetime.now(UTC),
        training_job_id=training_job_id,
    )
    db_session.add(version)
    db_session.flush()
    db_session.add(
        ModelMetric(
            model_version_id=version.id,
            fold_index=None,
            validation_season_label=None,
            mae=Decimal(mae),
            rmse=Decimal(mae),
            mean_bias=Decimal("0.0"),
            spearman_rank_corr=Decimal("0.7"),
            top_20_capture_rate=Decimal("0.5"),
            n_observations=100,
        )
    )
    db_session.commit()
    return version


def test_substitutes_best_live_capable_sibling_when_champion_is_live_incapable(db_session):
    _clear_existing_champion(db_session)
    training_job_id = _seed_job(db_session)
    # Champion (lowest MAE overall) is the live-incapable family.
    champion = _seed_candidate(
        db_session,
        training_job_id=training_job_id,
        family=ModelFamily.BASELINE_RULES_EP_NEXT,
        mae="0.9000",
        is_champion=True,
    )
    # Two live-capable siblings from the SAME training run, one clearly better than the other.
    _seed_candidate(
        db_session,
        training_job_id=training_job_id,
        family=ModelFamily.DECISION_TREE,
        mae="1.2000",
        is_champion=False,
    )
    best_live_capable = _seed_candidate(
        db_session,
        training_job_id=training_job_id,
        family=ModelFamily.RANDOM_FOREST,
        mae="0.9500",
        is_champion=False,
    )

    loaded = load_current_champion(db_session)

    assert loaded is not None
    assert loaded.model_family == ModelFamily.RANDOM_FOREST
    assert loaded.model_version_id == best_live_capable.id
    assert loaded.model_version_id != champion.id


def test_returns_none_when_champion_is_live_incapable_and_no_sibling_exists(db_session):
    _clear_existing_champion(db_session)
    training_job_id = _seed_job(db_session)
    _seed_candidate(
        db_session,
        training_job_id=training_job_id,
        family=ModelFamily.BASELINE_RULES_EP_NEXT,
        mae="0.9000",
        is_champion=True,
    )

    loaded = load_current_champion(db_session)

    assert loaded is None


def test_does_not_substitute_when_champion_is_already_live_capable(db_session):
    _clear_existing_champion(db_session)
    training_job_id = _seed_job(db_session)
    champion = _seed_candidate(
        db_session,
        training_job_id=training_job_id,
        family=ModelFamily.RANDOM_FOREST,
        mae="0.9000",
        is_champion=True,
    )
    # A live-incapable sibling that scores even better must NOT be preferred --
    # substitution only ever triggers when the FLAGGED champion itself can't run live.
    _seed_candidate(
        db_session,
        training_job_id=training_job_id,
        family=ModelFamily.BASELINE_RULES_EP_NEXT,
        mae="0.5000",
        is_champion=False,
    )

    loaded = load_current_champion(db_session)

    assert loaded is not None
    assert loaded.model_version_id == champion.id
    assert loaded.model_family == ModelFamily.RANDOM_FOREST
