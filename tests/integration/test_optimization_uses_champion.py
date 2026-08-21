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
from app.optimization.champion_loader import load_current_champion
from app.optimization.gameweek_resolution import resolve_target_gameweek
from app.optimization.projection import is_cold_start_season, project_player_with_champion
from packages.database.models.gameweek import Gameweek
from packages.database.models.model_version import ModelVersion
from packages.database.models.optimization_run_player import OptimizationRunPlayer
from packages.database.models.player import Player
from packages.database.models.player_features import PlayerFeatures
from packages.database.models.player_gameweek_stats import PlayerGameweekStats
from packages.database.models.season import Season
from packages.domain.jobs import JobStatus, JobType, TriggerType
from packages.domain.optimization import TargetGameweekStatus


def _seed_one_gameweek_stat_row_per_player_so_this_is_not_a_cold_start(db_session, season_id):
    """project_player_with_champion refuses to trust the champion at all for a cold-start
    player (zero player_gameweek_stats rows OF THEIR OWN -- see app.optimization.projection's
    own _is_cold_start_player). Seeds a row for EVERY player in the season (not just one) so
    every squad-optimization candidate is individually eligible for the champion path -- a
    single seeded row used to be enough back when the cold-start check was season-wide
    (_is_cold_start_season: "has ANYONE played yet"), but under the current per-player check
    only the one seeded player would be champion-eligible, and the solver has no reason to
    actually pick that specific player into its top-15 -- this test is about the champion path
    being reachABLE for a normal, real candidate pool, not about which exact player gets picked.
    """
    players = db_session.execute(select(Player).where(Player.season_id == season_id)).scalars().all()
    gameweek = db_session.execute(
        select(Gameweek).where(Gameweek.season_id == season_id).limit(1)
    ).scalar_one()
    for player in players:
        db_session.add(
            PlayerGameweekStats(
                player_id=player.id,
                season_id=season_id,
                gameweek_id=gameweek.id,
                total_points=2,
                xp_estimate=2.0,
                data_source="test",
            )
        )
    db_session.commit()


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
    current_season_id = db_session.execute(select(Season.id).where(Season.label == season_label)).scalar_one()
    _seed_one_gameweek_stat_row_per_player_so_this_is_not_a_cold_start(db_session, current_season_id)

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

    champion_sourced = [rp for rp in run_players if "_heuristic" not in rp.projection_source]
    assert len(champion_sourced) > 0, (
        "expected at least one squad candidate to use the champion-sourced projection path; "
        f"sources seen: {sorted({rp.projection_source for rp in run_players})}"
    )
    for rp in champion_sourced:
        assert rp.projection_source.startswith("horizon_")


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

    # Every candidate's horizon-summed source must be labeled "heuristic" -- never a real
    # champion family name -- when no champion has ever been selected (see
    # app.jobs.handlers.squad_optimization._build_candidates's own champion_label logic).
    for rp in run_players:
        assert rp.projection_source.endswith("_heuristic")
        assert "ml_champion_" not in rp.projection_source


def test_project_player_with_champion_uses_cold_start_blend_for_a_player_with_no_rows_of_their_own(
    db_session,
    patched_historical_fpl_client,
    patched_fpl_client_optimization_with_active_gameweek,
):
    """The architectural gap this closes: app.optimization.projection._is_cold_start_season checks
    the whole SEASON, so the moment even one player anywhere had one gameweek of real data, every
    OTHER player -- including ones with zero rows of their own -- used to get routed to the
    champion model's raw prediction, an input shape (every rolling/within-season feature still
    NaN) it was never meaningfully trained on. _is_cold_start_player fixes this by deciding per
    player instead. Seeds a current-season row for exactly ONE player, then asserts a SECOND,
    untouched player still gets the cold-start blend even though the season overall is no longer
    a cold-start season."""
    _seed_historical_champion(db_session)

    ingestion_job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert ingestion_job.status == JobStatus.SUCCEEDED
    season_label = ingestion_job.output_json["season_label"]
    current_season_id = db_session.execute(select(Season.id).where(Season.label == season_label)).scalar_one()

    players = (
        db_session.execute(select(Player).where(Player.season_id == current_season_id).order_by(Player.id))
        .scalars()
        .all()
    )
    warm_player, cold_player = players[0], players[1]
    gameweek = db_session.execute(
        select(Gameweek).where(Gameweek.season_id == current_season_id).limit(1)
    ).scalar_one()
    # Explicitly clear any pre-existing rows/state for these two players first, mirroring this
    # file's own "no champion" test's precedent (see module docstring) rather than assuming a
    # pristine DB -- this project's integration tests run against a shared, never-truncated
    # Postgres, and ``current_season_id``/its players are reused (get-or-create by season label)
    # across every test in the suite that seeds this same "current" season, so an earlier test run
    # may have already given one of these two positionally-selected players a stats row of their
    # own, OR (real, observed failure) an excluded ``status`` (e.g. an injury/availability-scaling
    # test elsewhere mutates an arbitrary player's own ``status``/``chance_of_playing_next_round``
    # on this same shared season) -- either would make ``warm_player``'s projection resolve to
    # ``"unavailable_status_excluded"`` instead of the champion path this test is actually about,
    # a failure mode unrelated to the per-player cold-start boundary being tested here. Forcing
    # both players back to a known-available state makes this test's own assertions test exactly
    # what they claim to, regardless of what any other test in the same run left behind.
    warm_player.status = "a"
    warm_player.chance_of_playing_next_round = None
    cold_player.status = "a"
    cold_player.chance_of_playing_next_round = None
    db_session.execute(
        delete(PlayerGameweekStats).where(
            PlayerGameweekStats.player_id.in_([warm_player.id, cold_player.id]),
            PlayerGameweekStats.season_id == current_season_id,
        )
    )
    db_session.commit()
    db_session.add(
        PlayerGameweekStats(
            player_id=warm_player.id,
            season_id=current_season_id,
            gameweek_id=gameweek.id,
            total_points=2,
            xp_estimate=2.0,
            data_source="test",
        )
    )
    db_session.commit()

    # The season overall has genuinely moved on -- proves the test below isn't accidentally
    # exercising the (already well-covered) whole-season cold-start path instead.
    assert is_cold_start_season(db_session, current_season_id) is False

    champion = load_current_champion(db_session)
    assert champion is not None
    target_gameweek_id, status = resolve_target_gameweek(db_session, current_season_id)
    assert status == TargetGameweekStatus.RESOLVED

    cold_result = project_player_with_champion(db_session, cold_player, target_gameweek_id, champion)
    assert not cold_result.source.startswith("ml_champion_"), (
        f"player with zero player_gameweek_stats rows of their own must never reach the champion "
        f"model just because SOME other player in the season has data; got source="
        f"{cold_result.source!r}"
    )

    warm_result = project_player_with_champion(db_session, warm_player, target_gameweek_id, champion)
    assert warm_result.source.startswith("ml_champion_"), (
        f"player WITH a current-season row of their own must be champion-eligible, proving the "
        f"cutover is a real per-player boundary rather than every player falling back to the "
        f"cold-start blend; got source={warm_result.source!r}"
    )
