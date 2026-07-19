"""Integration tests for Milestone 3 Layer 4: champion-aware optimizer projections.

Seeds a real trained champion (HISTORICAL_DATA_INGESTION + FEATURE_GENERATION
+ MODEL_TRAINING against the committed ``tests/fixtures/historical/2023-24/``
fixture -- the same one Layer 3's own ``test_model_training_job.py`` uses)
alongside a live "current" season with one unfinished gameweek
(``patched_fpl_client_optimization_with_active_gameweek``), then runs
SQUAD_OPTIMIZATION and asserts at least one ``optimization_run_players`` row
is champion-sourced.

Also asserts the pre-existing, no-champion fallback path is completely
unchanged: this project's integration tests run against a shared, persistent
local Postgres (see conftest.py's module docstring), so an earlier
test/module run may already have flagged a champion by the time this file's
"no champion" test runs -- it explicitly clears ``is_champion`` first rather
than assuming a pristine DB, to deterministically exercise the "no champion
has ever been selected" path.

The "mirror the real pipeline" ``FEATURE_GENERATION`` call in
``test_squad_optimization_uses_champion_projection_when_present`` below
deletes its own ``player_features`` rows immediately after confirming the
run itself succeeded (its own output is never otherwise read by this test) --
without that cleanup, those rows leak into ``app.modeling.walk_forward
.usable_season_labels_with_features``, a GLOBAL (not season-scoped) lookup
``MODEL_TRAINING``/``BACKTEST`` both rely on to mean "exactly the
intentionally-seeded historical season(s)" -- a real cross-test-pollution bug
surfaced by running the full integration suite TWICE against the same,
non-truncated Postgres (Milestone 7 Layer 6's own validation protocol; see
``tests/integration/test_model_training_job.py``'s and
``tests/integration/test_backtest_job.py``'s own single-usable-season
assertions, both of which this pollution would otherwise break on a second
run).
"""

import uuid

from sqlalchemy import delete, select, update

from app.jobs.service import execute_job
from packages.database.models.model_version import ModelVersion
from packages.database.models.optimization_run_player import OptimizationRunPlayer
from packages.database.models.player_features import PlayerFeatures
from packages.database.models.season import Season
from packages.domain.jobs import JobStatus, JobType, TriggerType
from packages.domain.optimization import TargetGameweekStatus


def _seed_historical_champion(db_session):
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

    training_job = execute_job(db_session, job_type=JobType.MODEL_TRAINING, trigger_type=TriggerType.LOCAL)
    assert training_job.status == JobStatus.SUCCEEDED
    return training_job


def _run_players_for(db_session, optimization_run_id):
    return (
        db_session.execute(
            select(OptimizationRunPlayer).where(
                OptimizationRunPlayer.optimization_run_id == optimization_run_id
            )
        )
        .scalars()
        .all()
    )


def test_squad_optimization_uses_champion_projection_when_present(
    db_session,
    patched_historical_fpl_client,
    patched_fpl_client_optimization_with_active_gameweek,
):
    _seed_historical_champion(db_session)

    ingestion_job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert ingestion_job.status == JobStatus.SUCCEEDED
    season_label = ingestion_job.output_json["season_label"]

    # Not strictly required by project_player_with_champion (it computes a
    # live feature row on the fly via app.features.service, never reads a
    # materialized player_features row) -- run anyway to mirror the real,
    # full production pipeline and confirm it doesn't interfere.
    features_job = execute_job(
        db_session,
        job_type=JobType.FEATURE_GENERATION,
        parameters={"season_label": season_label},
        trigger_type=TriggerType.LOCAL,
    )
    assert features_job.status == JobStatus.SUCCEEDED

    # Delete this call's own player_features rows immediately -- its own output is never otherwise
    # read by this test, and left in place they leak into every OTHER test's own GLOBAL
    # usable_season_labels_with_features query (see this module's own docstring for the real
    # cross-test-pollution bug this prevents).
    season_id = db_session.execute(select(Season.id).where(Season.label == season_label)).scalar_one()
    db_session.execute(delete(PlayerFeatures).where(PlayerFeatures.season_id == season_id))
    db_session.commit()

    squad_job = execute_job(db_session, job_type=JobType.SQUAD_OPTIMIZATION, trigger_type=TriggerType.LOCAL)
    assert squad_job.status == JobStatus.SUCCEEDED
    assert squad_job.output_json["target_gameweek_status"] == TargetGameweekStatus.RESOLVED.value

    optimization_run_id = uuid.UUID(squad_job.output_json["optimization_run_id"])
    run_players = _run_players_for(db_session, optimization_run_id)
    assert len(run_players) == 15

    champion_sourced = [rp for rp in run_players if rp.projection_source.startswith("ml_champion_")]
    assert len(champion_sourced) > 0, (
        "expected at least one squad candidate to use the champion-sourced projection path; "
        f"sources seen: {sorted({rp.projection_source for rp in run_players})}"
    )


def test_squad_optimization_falls_back_to_formula_when_no_champion_exists(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    # Shared, persistent local Postgres (see conftest.py's module docstring)
    # -- explicitly clear is_champion rather than assume a pristine DB.
    db_session.execute(update(ModelVersion).values(is_champion=False))
    db_session.commit()

    ingestion_job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert ingestion_job.status == JobStatus.SUCCEEDED

    squad_job = execute_job(db_session, job_type=JobType.SQUAD_OPTIMIZATION, trigger_type=TriggerType.LOCAL)
    assert squad_job.status == JobStatus.SUCCEEDED
    # A target gameweek IS resolved here (unlike the plain
    # patched_fpl_client_optimization fixture's tests) -- proving the
    # fallback below is genuinely "no champion", not "no target gameweek".
    assert squad_job.output_json["target_gameweek_status"] == TargetGameweekStatus.RESOLVED.value

    optimization_run_id = uuid.UUID(squad_job.output_json["optimization_run_id"])
    run_players = _run_players_for(db_session, optimization_run_id)
    assert len(run_players) == 15

    # Every candidate must fall back to project_player's own source strings --
    # never a champion-sourced one -- when no champion has ever been selected.
    allowed_fallback_sources = {
        "ep_next",
        "ep_next_availability_scaled",
        "points_per_game",
        "points_per_game_availability_scaled",
        "no_data_zero",
    }
    for rp in run_players:
        assert not rp.projection_source.startswith("ml_champion_")
        assert rp.projection_source in allowed_fallback_sources
