"""Integration tests for COLD_START_PROJECTION_VALIDATION (Milestone 8: historical validation of
``app.optimization.projection._cold_start_projection``'s hand-written blend formula).

Two groups:

1. Direct tests of ``app.modeling.cold_start_validation.build_player_season_observations`` against
   hand-seeded ``Season``/``Gameweek``/``Player``/``PlayerGameweekStats``/``Fixture`` rows -- the
   only way to exercise the real cross-season leakage exclusion (``filter_to_strictly_prior``
   against real DB-backed data) and the DB-dependent boundary cases (no prior season at all, prior
   history with none in the early window). Failure/boundary cases come first per this repo's own
   test-priority convention.
2. End-to-end tests of the job itself via ``execute_job``, seeded via
   ``HISTORICAL_DATA_INGESTION`` against the committed ``2023-24`` CSV fixture ingested under two
   different season labels (``patched_historical_fpl_client`` returns the same fixture content
   regardless of which ``season_label`` is requested -- see that fixture's own docstring), proving
   the full job runs end-to-end and persists a real ``cold_start_validation_runs`` row.
"""

import itertools
import secrets
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.jobs.service import execute_job
from app.modeling.cold_start_validation import (
    build_player_season_observations,
    build_true_rookie_observations,
    compute_price_comparable_buckets,
    sweep_price_prior_blend_weight_candidates,
)
from packages.database.models.cold_start_validation_run import ColdStartValidationRun
from packages.database.models.fixture import Fixture
from packages.database.models.gameweek import Gameweek
from packages.database.models.player import Player
from packages.database.models.player_gameweek_stats import PlayerGameweekStats
from packages.database.models.season import Season
from packages.database.models.team import Team
from packages.domain.jobs import JobStatus, JobType, TriggerType
from packages.domain.learning import ColdStartValidationRunStatus

# Same rationale/precedent as tests/integration/conftest.py's own _season_year_counter (not
# imported cross-module -- module-private names aren't shared between test modules in this repo,
# see e.g. app.jobs.handlers.backtest's identical duplication precedent): tests/integration runs
# against a real, never-truncated, shared Postgres, and this repo's own protocol runs the suite
# TWICE per invocation -- a hardcoded literal season label here would collide with itself on the
# second run. secrets.randbelow gives each process a genuinely (not just practically) unique block.
_season_year_counter = itertools.count(2100 + secrets.randbelow(7899))


def _next_season_label() -> str:
    year = next(_season_year_counter)
    return f"{year}-{str(year + 1)[-2:]}"


def _make_season(db_session, label: str | None = None) -> Season:
    label = label or _next_season_label()
    season = Season(label=label, is_current=False)
    db_session.add(season)
    db_session.commit()
    db_session.refresh(season)
    return season


def _make_team(db_session, season: Season, fpl_team_id: int) -> Team:
    team = Team(
        fpl_team_id=fpl_team_id,
        season_id=season.id,
        name=f"Team {fpl_team_id}",
        short_name=f"T{fpl_team_id}",
        last_synced_at=datetime.now(UTC),
    )
    db_session.add(team)
    db_session.commit()
    db_session.refresh(team)
    return team


def _make_gameweeks(db_session, season: Season, year: int, count: int = 6) -> list[Gameweek]:
    gameweeks = []
    for gw_num in range(1, count + 1):
        gw = Gameweek(
            fpl_event_id=gw_num,
            season_id=season.id,
            name=f"Gameweek {gw_num}",
            deadline_time=datetime(year, 8, gw_num, 18, 0, tzinfo=UTC),
            finished=True,
            last_synced_at=datetime.now(UTC),
        )
        db_session.add(gw)
        gameweeks.append(gw)
    db_session.commit()
    for gw in gameweeks:
        db_session.refresh(gw)
    return gameweeks


def _make_player(db_session, season: Season, fpl_code: int, fpl_element_id: int = 1) -> Player:
    player = Player(
        fpl_element_id=fpl_element_id,
        fpl_code=fpl_code,
        season_id=season.id,
        now_cost=50,
        last_synced_at=datetime.now(UTC),
    )
    db_session.add(player)
    db_session.commit()
    db_session.refresh(player)
    return player


def _add_stats(
    db_session, player: Player, season: Season, gameweeks: list[Gameweek], points_per_gw: int, fixture=None
) -> None:
    for gw in gameweeks:
        db_session.add(
            PlayerGameweekStats(
                player_id=player.id,
                season_id=season.id,
                gameweek_id=gw.id,
                fixture_id=fixture.id if fixture is not None else None,
                was_home=True if fixture is not None else None,
                total_points=points_per_gw,
                minutes=90,
                data_source="test_fixture",
            )
        )
    db_session.commit()


def _add_stats_with_price(
    db_session,
    player,
    season,
    gameweeks,
    points_per_gw: int,
    price: int,
    position_short_name: str,
    fixture=None,
) -> None:
    """Same as ``_add_stats`` but also sets ``value_at_time``/``position_short_name_at_time`` --
    the two columns ``build_true_rookie_observations``/``compute_price_comparable_buckets`` need
    and ``_add_stats`` deliberately leaves null (every OTHER test in this file doesn't care about
    price/position)."""
    for gw in gameweeks:
        db_session.add(
            PlayerGameweekStats(
                player_id=player.id,
                season_id=season.id,
                gameweek_id=gw.id,
                fixture_id=fixture.id if fixture is not None else None,
                was_home=True if fixture is not None else None,
                total_points=points_per_gw,
                minutes=90,
                value_at_time=price,
                position_short_name_at_time=position_short_name,
                data_source="test_fixture",
            )
        )
    db_session.commit()


# --- Leakage discipline: the critical case (would fail if leakage prevention were broken) -----------


def test_build_player_season_observations_never_leaks_the_target_seasons_own_rows(db_session):
    prior_season = _make_season(db_session)
    target_season = _make_season(db_session)
    prior_gws = _make_gameweeks(db_session, prior_season, 2019)
    target_gws = _make_gameweeks(db_session, target_season, 2020)

    fpl_code = secrets.randbelow(10**8)
    prior_player = _make_player(db_session, prior_season, fpl_code, fpl_element_id=1)
    target_player = _make_player(db_session, target_season, fpl_code, fpl_element_id=1)

    # Real prior-season signal: consistently low (2 points/gw).
    _add_stats(db_session, prior_player, prior_season, prior_gws, points_per_gw=2)
    # The target season's OWN rows are deliberately extreme (100 points/gw) -- if these leaked into
    # "prior" data for their own season's prediction, the prediction would land close to 100
    # (near-zero error vs. the season's own actual). If leakage prevention holds, the prediction is
    # computed from the prior season alone and lands close to 2 (a large, real error vs. actual).
    _add_stats(db_session, target_player, target_season, target_gws, points_per_gw=100)

    observations, usable = build_player_season_observations(db_session)

    # Membership + relative order, not exact list equality -- this repo's own integration tests run
    # against a real, never-truncated, shared Postgres, so other tests' own seasons may coexist here.
    assert prior_season.label in usable and target_season.label in usable
    assert usable.index(prior_season.label) < usable.index(target_season.label)
    obs = next(o for o in observations if o.fpl_code == fpl_code)
    assert obs.season_label == target_season.label
    assert obs.actual_early_season_avg == 100.0

    # The leakage-critical assertion: prior_stats must contain ONLY the prior season's own rows.
    prior_season_ids = {stat.season_id for stat in obs.prior_stats}
    assert prior_season_ids == {str(prior_season.id)}
    assert str(target_season.id) not in prior_season_ids

    # And the predicted value (from prior_stats alone) must be far from the target season's own
    # actual -- proof no leak occurred (a leak would make this near 100, not near 2).
    from app.features import engineering

    predicted = engineering.career_avg_early_season_points_prior_seasons(obs.prior_stats, 6, 1.0)
    assert predicted == 2.0
    assert abs(float(predicted) - obs.actual_early_season_avg) > 90


# --- Boundary cases: no prior season at all; prior history with nothing in the early window ---------


def test_build_player_season_observations_earliest_usable_season_never_becomes_a_target(db_session):
    only_season = _make_season(db_session)
    gameweeks = _make_gameweeks(db_session, only_season, 2021)
    fpl_code = secrets.randbelow(10**8)
    player = _make_player(db_session, only_season, fpl_code=fpl_code)
    _add_stats(db_session, player, only_season, gameweeks, points_per_gw=5)

    observations, usable = build_player_season_observations(db_session)

    assert only_season.label in usable  # usable (has stats), but...
    # ...never a target: no strictly-prior season exists to predict this specific player from.
    assert all(o.fpl_code != fpl_code for o in observations)


def test_build_player_season_observations_skips_player_whose_prior_history_misses_the_early_window(
    db_session,
):
    prior_season = _make_season(db_session)
    target_season = _make_season(db_session)
    target_gws = _make_gameweeks(db_session, target_season, 2020)
    # Prior season gameweeks numbered 30-35 (late season only) -- real prior history exists, but
    # none of it falls within the early-season cutoff (fpl_event_id <= 6).
    late_gws = []
    for i, gw_num in enumerate(range(30, 36)):
        gw = Gameweek(
            fpl_event_id=gw_num,
            season_id=prior_season.id,
            name=f"Gameweek {gw_num}",
            deadline_time=datetime(2020, 3, 1 + i, tzinfo=UTC),
            finished=True,
            last_synced_at=datetime.now(UTC),
        )
        db_session.add(gw)
        late_gws.append(gw)
    db_session.commit()
    for gw in late_gws:
        db_session.refresh(gw)

    fpl_code = secrets.randbelow(10**8)
    prior_player = _make_player(db_session, prior_season, fpl_code, fpl_element_id=1)
    target_player = _make_player(db_session, target_season, fpl_code, fpl_element_id=1)
    _add_stats(db_session, prior_player, prior_season, late_gws, points_per_gw=8)
    _add_stats(db_session, target_player, target_season, target_gws, points_per_gw=4)

    observations, _usable = build_player_season_observations(db_session)

    assert all(o.fpl_code != fpl_code for o in observations)


# --- Happy path: real difficulty-adjusted per-gameweek data flows through -----------------------------


def test_build_player_season_observations_attaches_real_early_gameweek_fixture_difficulty(db_session):
    prior_season = _make_season(db_session)
    target_season = _make_season(db_session)
    prior_gws = _make_gameweeks(db_session, prior_season, 2019)
    target_gws = _make_gameweeks(db_session, target_season, 2020)

    home_team = _make_team(db_session, target_season, fpl_team_id=1)
    away_team = _make_team(db_session, target_season, fpl_team_id=2)
    fixture = Fixture(
        fpl_fixture_id=1,
        season_id=target_season.id,
        gameweek_id=target_gws[0].id,
        home_team_id=home_team.id,
        away_team_id=away_team.id,
        home_difficulty=2,
        away_difficulty=4,
        kickoff_time=target_gws[0].deadline_time,
        finished=True,
        last_synced_at=datetime.now(UTC),
    )
    db_session.add(fixture)
    db_session.commit()
    db_session.refresh(fixture)

    fpl_code = secrets.randbelow(10**8)
    prior_player = _make_player(db_session, prior_season, fpl_code, fpl_element_id=1)
    target_player = _make_player(db_session, target_season, fpl_code, fpl_element_id=1)
    target_player.team_id = home_team.id
    db_session.commit()

    _add_stats(db_session, prior_player, prior_season, prior_gws, points_per_gw=3)
    db_session.add(
        PlayerGameweekStats(
            player_id=target_player.id,
            season_id=target_season.id,
            gameweek_id=target_gws[0].id,
            fixture_id=fixture.id,
            was_home=True,
            total_points=9,
            minutes=90,
            data_source="test_fixture",
        )
    )
    db_session.commit()

    observations, _usable = build_player_season_observations(db_session)

    obs = next(o for o in observations if o.fpl_code == fpl_code)
    early = obs.early_gameweek_actuals
    assert len(early) == 1
    assert early[0].fpl_event_id == 1
    assert early[0].actual_points == 9
    assert early[0].difficulty == 2  # home_difficulty, since was_home=True


# --- Price-informed true-rookie fallback: build_true_rookie_observations -----------------------------
# Boundary/failure cases first, per this repo's own test-priority convention.


def test_build_true_rookie_observations_skips_player_with_real_prior_season_history(db_session):
    """A player WITH prior-season history belongs to build_player_season_observations's population,
    not this one -- the mirror-image of that function's own "requires non-empty prior_stats" gate."""
    prior_season = _make_season(db_session)
    target_season = _make_season(db_session)
    prior_gws = _make_gameweeks(db_session, prior_season, 2032)
    target_gws = _make_gameweeks(db_session, target_season, 2033)

    fpl_code = secrets.randbelow(10**8)
    prior_player = _make_player(db_session, prior_season, fpl_code, fpl_element_id=1)
    target_player = _make_player(db_session, target_season, fpl_code, fpl_element_id=1)
    _add_stats(db_session, prior_player, prior_season, prior_gws, points_per_gw=3)
    _add_stats_with_price(
        db_session,
        target_player,
        target_season,
        target_gws,
        points_per_gw=4,
        price=50,
        position_short_name="MID",
    )

    observations, _usable = build_true_rookie_observations(db_session)

    assert all(o.fpl_code != fpl_code for o in observations)


def test_build_true_rookie_observations_skips_when_no_resolvable_price(db_session):
    prior_season = _make_season(db_session)
    target_season = _make_season(db_session)
    prior_gws = _make_gameweeks(db_session, prior_season, 2034)
    target_gws = _make_gameweeks(db_session, target_season, 2035)

    other_fpl_code = secrets.randbelow(10**8)
    other_prior_player = _make_player(db_session, prior_season, other_fpl_code, fpl_element_id=1)
    _add_stats(db_session, other_prior_player, prior_season, prior_gws, points_per_gw=5)

    rookie_fpl_code = secrets.randbelow(10**8)
    rookie = _make_player(db_session, target_season, rookie_fpl_code, fpl_element_id=2)
    _add_stats(db_session, rookie, target_season, target_gws, points_per_gw=6)  # no value_at_time set

    observations, _usable = build_true_rookie_observations(db_session)

    assert all(o.fpl_code != rookie_fpl_code for o in observations)


def test_build_true_rookie_observations_includes_a_player_with_zero_prior_season_history(db_session):
    prior_season = _make_season(db_session)
    target_season = _make_season(db_session)
    prior_gws = _make_gameweeks(db_session, prior_season, 2030)
    target_gws = _make_gameweeks(db_session, target_season, 2031)

    # An unrelated, established player in the prior season -- needed so the prior season counts as
    # "usable" (has real player_gameweek_stats rows), but is NOT this test's true rookie.
    other_fpl_code = secrets.randbelow(10**8)
    other_prior_player = _make_player(db_session, prior_season, other_fpl_code, fpl_element_id=1)
    _add_stats(db_session, other_prior_player, prior_season, prior_gws, points_per_gw=5)

    rookie_fpl_code = secrets.randbelow(10**8)
    rookie = _make_player(db_session, target_season, rookie_fpl_code, fpl_element_id=2)
    _add_stats_with_price(
        db_session, rookie, target_season, target_gws, points_per_gw=6, price=70, position_short_name="FWD"
    )

    observations, usable = build_true_rookie_observations(db_session)

    assert prior_season.label in usable and target_season.label in usable
    obs = next(o for o in observations if o.fpl_code == rookie_fpl_code)
    assert obs.season_label == target_season.label
    assert obs.actual_early_season_avg == 6.0
    assert obs.price_at_prediction_time == 70
    assert obs.position_short_name == "FWD"


# --- Price-informed true-rookie fallback: compute_price_comparable_buckets ---------------------------


def test_compute_price_comparable_buckets_position_scopes_real_averages(db_session):
    season = _make_season(db_session)
    gws = _make_gameweeks(db_session, season, 2040)

    cheap_code = secrets.randbelow(10**8)
    cheap = _make_player(db_session, season, cheap_code, fpl_element_id=1)
    _add_stats_with_price(
        db_session, cheap, season, gws, points_per_gw=1, price=40, position_short_name="DEF"
    )

    pricey_code = secrets.randbelow(10**8)
    pricey = _make_player(db_session, season, pricey_code, fpl_element_id=2)
    _add_stats_with_price(
        db_session, pricey, season, gws, points_per_gw=5, price=80, position_short_name="DEF"
    )

    buckets, usable = compute_price_comparable_buckets(db_session, n_buckets_per_position=2)

    assert season.label in usable
    def_buckets = [b for b in buckets if b.position_short_name == "DEF"]
    # Membership, not exact count/equality -- this repo's own tests/integration protocol runs
    # against a real, never-truncated, shared Postgres, so other tests may add further DEF entries.
    assert any(b.price_low <= 40 <= b.price_high for b in def_buckets)
    assert any(b.price_low <= 80 <= b.price_high for b in def_buckets)


# --- Price-informed true-rookie fallback: sweep_price_prior_blend_weight_candidates -------------------
# The critical leakage case (would fail if per-eval-season leakage prevention were broken) first.


def test_sweep_price_prior_blend_weight_candidates_never_leaks_target_seasons_own_pool_data(db_session):
    prior_season = _make_season(db_session)
    target_season = _make_season(db_session)
    prior_gws = _make_gameweeks(db_session, prior_season, 2050)
    target_gws = _make_gameweeks(db_session, target_season, 2051)

    # Prior season's own pool: a real DEF player at price 45, real early-season output 1.0.
    prior_pool_code = secrets.randbelow(10**8)
    prior_pool_player = _make_player(db_session, prior_season, prior_pool_code, fpl_element_id=1)
    _add_stats_with_price(
        db_session,
        prior_pool_player,
        prior_season,
        prior_gws,
        points_per_gw=1,
        price=45,
        position_short_name="DEF",
    )

    # The TARGET season's own pool: an unrelated DEF player at the SAME price bracket (45), but with
    # an extreme real output (20/gw). If this leaked into the bucket used to evaluate the target
    # season's own true rookie, the bucket average would be dragged sharply towards 20.
    target_pool_code = secrets.randbelow(10**8)
    target_pool_player = _make_player(db_session, target_season, target_pool_code, fpl_element_id=2)
    _add_stats_with_price(
        db_session,
        target_pool_player,
        target_season,
        target_gws,
        points_per_gw=20,
        price=45,
        position_short_name="DEF",
    )

    # The true rookie being evaluated: same season, same position/price bracket, real output close
    # to the (non-leaked) PRIOR season's own average (1.0), far from the target season's own (20.0).
    rookie_code = secrets.randbelow(10**8)
    rookie = _make_player(db_session, target_season, rookie_code, fpl_element_id=3)
    _add_stats_with_price(
        db_session, rookie, target_season, target_gws, points_per_gw=1, price=45, position_short_name="DEF"
    )
    rookie.ep_next = 1.0  # needed for sweep_price_prior_blend_weight_candidates's own ep_next-proxy gate
    db_session.commit()

    observations, usable = build_true_rookie_observations(db_session)
    rookie_obs = next(o for o in observations if o.fpl_code == rookie_code)

    results = sweep_price_prior_blend_weight_candidates(db_session, [rookie_obs], usable, candidates=[1.0])

    # weight=1.0 -> the prediction is purely the bucket average. Leakage-safe, that average is built
    # from ONLY the prior season's pool (avg=1.0) -- close to the rookie's own real actual (1.0), a
    # small error. A leak (pulling in the target season's own 20/gw player) would drag the bucket
    # average up sharply, producing a large, easily-detectable error instead.
    assert results[0].n_observations >= 1
    assert results[0].mae is not None
    assert results[0].mae < 5.0


# --- Full job, end-to-end via execute_job -------------------------------------------------------------


def _seed_evaluable_seasons(db_session, n_players: int = 4) -> tuple[Season, Season]:
    """Direct ORM seed of two chronologically-real, cross-season-linked seasons with several
    players -- NOT via HISTORICAL_DATA_INGESTION/the committed CSV fixture, because that fixture's
    own gameweek deadlines are real 2023-24 calendar dates regardless of which ``season_label`` the
    ingestion job is asked to use (``patched_historical_fpl_client`` always returns the same file
    content -- see its own docstring), so re-ingesting it under two different labels produces two
    "seasons" with IDENTICAL (not chronologically prior/later) ``Gameweek.deadline_time`` values.
    ``filter_to_strictly_prior`` correctly (and was, in fact, caught by this while writing this test)
    treats identical deadlines as not-strictly-prior and excludes them -- exactly the leakage-safe
    behavior it's supposed to have, but it means that approach can't produce real evaluable
    observations for this job-level test. Direct seeding sidesteps this entirely by giving the two
    seasons real, distinct calendar years.
    """
    prior_season = _make_season(db_session)
    target_season = _make_season(db_session)
    prior_gws = _make_gameweeks(db_session, prior_season, 2010)
    target_gws = _make_gameweeks(db_session, target_season, 2011)

    for i in range(n_players):
        fpl_code = secrets.randbelow(10**8)
        prior_player = _make_player(db_session, prior_season, fpl_code, fpl_element_id=i + 1)
        target_player = _make_player(db_session, target_season, fpl_code, fpl_element_id=i + 1)
        _add_stats(db_session, prior_player, prior_season, prior_gws, points_per_gw=2 + i)
        _add_stats(db_session, target_player, target_season, target_gws, points_per_gw=3 + i)

    return prior_season, target_season


def _run_cold_start_validation_job(db_session, parameters: dict | None = None):
    return execute_job(
        db_session,
        job_type=JobType.COLD_START_PROJECTION_VALIDATION,
        parameters=parameters,
        trigger_type=TriggerType.LOCAL,
    )


# NOTE: unlike BACKTEST's own analogous NO_SEASONS_WITH_FEATURES/NO_FOLD_SCORED refusal paths,
# NOT_ENOUGH_USABLE_SEASONS/NO_EVALUABLE_OBSERVATIONS are deliberately not integration-tested here
# either -- this repo's own tests/integration protocol runs against a real, never-truncated,
# shared Postgres, so "zero usable seasons exist" is not a reliably reproducible precondition once
# other tests in this same file (or a prior run) have already seeded seasons with real stats. Both
# paths are still exercised by construction (see the handler's own early-return checks) and code
# reviewed; see app.jobs.handlers.backtest's identical precedent for not testing this class of
# "the whole DB happens to be empty" precondition at the integration level.


def test_cold_start_validation_produces_real_sweep_results_end_to_end(db_session):
    prior_season, target_season = _seed_evaluable_seasons(db_session)

    job = _run_cold_start_validation_job(db_session)

    assert job.status == JobStatus.SUCCEEDED, job.error_message
    output = job.output_json
    # Membership, not exact equality -- other tests in this shared, never-truncated DB may have
    # seeded further usable seasons of their own.
    assert {prior_season.label, target_season.label}.issubset(set(output["usable_seasons"]))
    assert output["n_observations"] > 0
    assert len(output["half_life_sweep"]) == 6  # EARLY_SEASON_HALF_LIFE_SWEEP_CANDIDATES's own length
    assert output["best_half_life_mae"] is not None
    assert output["blend_ratio_caveat"] is not None

    row = db_session.get(ColdStartValidationRun, uuid.UUID(output["cold_start_validation_run_id"]))
    assert row is not None
    assert row.run_status == ColdStartValidationRunStatus.SUCCEEDED
    assert row.component_evaluated == "career_avg_early_season_points_prior_seasons_only"
    assert row.n_observations > 0
    assert row.best_half_life_mae is not None
    assert row.default_half_life_mae is not None
    assert row.started_at is not None
    assert row.completed_at is not None


def test_cold_start_validation_honors_custom_half_life_candidates(db_session):
    _seed_evaluable_seasons(db_session)

    job = _run_cold_start_validation_job(db_session, parameters={"half_life_candidates": [1.0, 5.0]})

    assert job.status == JobStatus.SUCCEEDED, job.error_message
    assert [r["half_life_seasons"] for r in job.output_json["half_life_sweep"]] == [1.0, 5.0]
    assert job.output_json["best_half_life_seasons"] in (1.0, 5.0)


def test_cold_start_validation_produces_price_prior_results_and_buckets_end_to_end(db_session):
    prior_season, target_season = _seed_evaluable_seasons(db_session)
    target_gws = (
        db_session.execute(
            select(Gameweek).where(Gameweek.season_id == target_season.id).order_by(Gameweek.fpl_event_id)
        )
        .scalars()
        .all()
    )

    rookie_code = secrets.randbelow(10**8)
    rookie = _make_player(db_session, target_season, rookie_code, fpl_element_id=99)
    _add_stats_with_price(
        db_session, rookie, target_season, target_gws, points_per_gw=4, price=55, position_short_name="MID"
    )

    job = _run_cold_start_validation_job(db_session)

    assert job.status == JobStatus.SUCCEEDED, job.error_message
    output = job.output_json
    assert output["n_true_rookie_observations"] >= 1
    assert len(output["price_prior_sweep"]) == 5  # PRICE_PRIOR_BLEND_WEIGHT_SWEEP_CANDIDATES's own length
    assert output["n_price_comparable_buckets"] > 0
    assert output["price_prior_caveat"] is not None

    row = db_session.get(ColdStartValidationRun, uuid.UUID(output["cold_start_validation_run_id"]))
    assert row is not None
    assert row.n_true_rookie_observations >= 1
    assert row.price_comparable_buckets_json  # not None/empty
    assert row.price_prior_caveat is not None
    assert row.default_price_prior_weight is not None


def test_cold_start_validation_rerun_creates_a_second_insert_only_row(db_session):
    _seed_evaluable_seasons(db_session)

    first_job = _run_cold_start_validation_job(db_session)
    assert first_job.status == JobStatus.SUCCEEDED
    second_job = _run_cold_start_validation_job(db_session)
    assert second_job.status == JobStatus.SUCCEEDED

    first_run_id = uuid.UUID(first_job.output_json["cold_start_validation_run_id"])
    second_run_id = uuid.UUID(second_job.output_json["cold_start_validation_run_id"])
    assert first_run_id != second_run_id
    assert db_session.get(ColdStartValidationRun, first_run_id) is not None
    assert db_session.get(ColdStartValidationRun, second_run_id) is not None
