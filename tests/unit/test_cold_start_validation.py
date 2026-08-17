"""Unit tests for app.modeling.cold_start_validation's sweep functions.

These test the three sweeps (half-life, fixture-difficulty coefficient, ep_next/career_avg blend
ratio) directly against hand-built ``PlayerSeasonObservation``/``EarlyGameweekActual`` instances --
no database needed, since the sweeps themselves are pure functions over already-assembled
observations. The DB-backed observation-BUILDING step (``build_player_season_observations``,
including the leakage-critical cross-season exclusion) is covered by
``tests/integration/test_cold_start_projection_validation_job.py`` instead, since it requires real
``Season``/``Gameweek``/``Player``/``PlayerGameweekStats`` rows.

Failure/boundary cases come first per this repo's own test-priority convention, happy paths last.
"""

from datetime import UTC, datetime

from app.features.engineering import PriorGameweekStat
from app.modeling.cold_start_validation import (
    EarlyGameweekActual,
    PlayerSeasonObservation,
    PriceComparableBucket,
    lookup_price_comparable_bucket,
    sweep_blend_ratio_candidates,
    sweep_difficulty_coefficient_candidates,
    sweep_half_life_candidates,
)


def _stat(season_id: str, fpl_event_id: int, total_points: int, deadline: datetime) -> PriorGameweekStat:
    return PriorGameweekStat(
        season_id=season_id,
        gameweek_key=f"{season_id}-{fpl_event_id}",
        gameweek_deadline=deadline,
        total_points=total_points,
        minutes=90,
        was_home=None,
        fixture_kickoff_time=None,
        fpl_event_id=fpl_event_id,
    )


def _early_prior_stats_for_season(
    season_id: str, year: int, points_per_gw: int
) -> tuple[PriorGameweekStat, ...]:
    return tuple(_stat(season_id, gw, points_per_gw, datetime(year, 8, gw, tzinfo=UTC)) for gw in range(1, 7))


def _observation(
    *,
    season_label: str = "2021-22",
    actual: float,
    prior_stats: tuple[PriorGameweekStat, ...],
    early_gameweek_actuals: tuple[EarlyGameweekActual, ...] = (),
    ep_next_proxy: float | None = None,
) -> PlayerSeasonObservation:
    return PlayerSeasonObservation(
        fpl_code=1,
        season_label=season_label,
        actual_early_season_avg=actual,
        prior_stats=prior_stats,
        early_gameweek_actuals=early_gameweek_actuals,
        ep_next_proxy=ep_next_proxy,
    )


# --- sweep_half_life_candidates: boundary/failure cases first ---------------------------------------


def test_sweep_half_life_candidates_empty_observations_returns_undefined_mae():
    results = sweep_half_life_candidates([], candidates=[1.0, 2.0])

    assert [r.n_observations for r in results] == [0, 0]
    assert all(r.mae is None for r in results)  # genuinely undefined, not a placeholder 0


def test_sweep_half_life_candidates_flat_control_reproduces_unweighted_mean_across_seasons():
    # Two prior seasons with very different early-season averages (2 vs 10 points/gw) -- a real,
    # meaningfully-differentiating scenario for recency weighting.
    prior = _early_prior_stats_for_season(
        "season-old", 2019, points_per_gw=2
    ) + _early_prior_stats_for_season("season-recent", 2020, points_per_gw=10)
    obs = _observation(actual=10.0, prior_stats=prior)

    results = sweep_half_life_candidates([obs], candidates=[0.5, 1_000_000.0])
    by_candidate = {r.half_life_seasons: r for r in results}

    # A short half-life (0.5) weights the more recent season heavily -> prediction close to 10 -> low MAE.
    # The flat control weights both seasons equally -> prediction = 6.0 -> MAE = 4.0.
    assert by_candidate[0.5].mae < by_candidate[1_000_000.0].mae
    assert by_candidate[1_000_000.0].mae == 4.0


def test_sweep_half_life_candidates_picks_lower_mae_for_the_better_fitting_half_life():
    # Player whose recent season strongly dominates their true current level (actual = 10) --
    # a short half-life should fit this better (lower MAE) than a long one.
    prior = _early_prior_stats_for_season(
        "season-old", 2018, points_per_gw=1
    ) + _early_prior_stats_for_season("season-recent", 2020, points_per_gw=10)
    obs = _observation(actual=10.0, prior_stats=prior)

    results = sweep_half_life_candidates([obs], candidates=[0.5, 3.0])
    by_candidate = {r.half_life_seasons: r for r in results}

    assert by_candidate[0.5].mae < by_candidate[3.0].mae


# --- sweep_difficulty_coefficient_candidates: boundary/failure cases first --------------------------


def test_sweep_difficulty_coefficient_candidates_no_early_gameweek_actuals_returns_zero_n():
    obs = _observation(
        actual=5.0, prior_stats=_early_prior_stats_for_season("s", 2020, 5), early_gameweek_actuals=()
    )

    results = sweep_difficulty_coefficient_candidates([obs], candidates=[0.0, 0.075])

    assert all(r.n_observations == 0 for r in results)


def test_sweep_difficulty_coefficient_candidates_zero_coefficient_ignores_difficulty():
    # Baseline (career_avg) will be 5.0 (flat prior). Two gameweeks at opposite difficulty extremes,
    # both with actual points equal to the baseline -- coefficient 0.0 should predict exactly 5.0 for
    # both regardless of difficulty, giving MAE == 0.
    prior = _early_prior_stats_for_season("s", 2020, 5)
    early = (
        EarlyGameweekActual(fpl_event_id=1, actual_points=5, difficulty=1),
        EarlyGameweekActual(fpl_event_id=2, actual_points=5, difficulty=5),
    )
    obs = _observation(actual=5.0, prior_stats=prior, early_gameweek_actuals=early)

    results = sweep_difficulty_coefficient_candidates([obs], candidates=[0.0], half_life_seasons=1.0)

    assert results[0].mae == 0.0
    assert results[0].n_observations == 2


def test_sweep_difficulty_coefficient_candidates_nonzero_coefficient_improves_mae_when_predictive():
    # Baseline = 4.0 (flat prior). Real signal: easy fixtures (difficulty=1) score above baseline,
    # hard fixtures (difficulty=5) score below it -- exactly what the adjustment is meant to capture.
    prior = _early_prior_stats_for_season("s", 2020, 4)
    early = (
        EarlyGameweekActual(fpl_event_id=1, actual_points=6, difficulty=1),
        EarlyGameweekActual(fpl_event_id=2, actual_points=2, difficulty=5),
    )
    obs = _observation(actual=4.0, prior_stats=prior, early_gameweek_actuals=early)

    results = sweep_difficulty_coefficient_candidates(
        [obs], candidates=[0.0, 0.075, 0.15], half_life_seasons=1.0
    )
    by_candidate = {r.coefficient: r for r in results}

    assert by_candidate[0.15].mae < by_candidate[0.0].mae
    assert by_candidate[0.075].mae < by_candidate[0.0].mae


# --- sweep_blend_ratio_candidates: boundary/failure cases first -------------------------------------


def test_sweep_blend_ratio_candidates_skips_observations_with_no_ep_next_proxy():
    obs_without_proxy = _observation(
        actual=5.0, prior_stats=_early_prior_stats_for_season("s", 2020, 5), ep_next_proxy=None
    )

    results = sweep_blend_ratio_candidates([obs_without_proxy], candidates=[0.5])

    assert results[0].n_observations == 0


def test_sweep_blend_ratio_candidates_full_weight_on_career_avg_matches_career_avg_alone():
    prior = _early_prior_stats_for_season("s", 2020, 6)  # career_avg == 6.0
    obs = _observation(actual=6.0, prior_stats=prior, ep_next_proxy=1.0)  # ep_next proxy is a bad estimate

    results = sweep_blend_ratio_candidates([obs], candidates=[1.0], half_life_seasons=1.0)

    assert results[0].mae == 0.0  # weight 1.0 on career_avg (6.0) exactly matches actual (6.0)
    assert results[0].ep_next_weight == 0.0


def test_sweep_blend_ratio_candidates_picks_lower_mae_when_ep_next_proxy_is_more_accurate():
    prior = _early_prior_stats_for_season("s", 2020, 2)  # career_avg == 2.0, a poor predictor here
    obs = _observation(actual=8.0, prior_stats=prior, ep_next_proxy=8.0)  # ep_next proxy is exact

    results = sweep_blend_ratio_candidates([obs], candidates=[0.3, 0.7], half_life_seasons=1.0)
    by_candidate = {r.career_avg_weight: r for r in results}

    # More weight on the (here, exact) ep_next proxy should win.
    assert by_candidate[0.3].mae < by_candidate[0.7].mae


# --- lookup_price_comparable_bucket: boundary/failure cases first -----------------------------------
#
# The DB-backed bucket-BUILDING step (_bucket_pool_entries, compute_price_comparable_buckets,
# build_true_rookie_observations, sweep_price_prior_blend_weight_candidates) requires real
# Season/Gameweek/Player/PlayerGameweekStats rows and is covered by
# tests/integration/test_cold_start_projection_validation_job.py instead, mirroring this file's own
# existing "DB-backed building vs pure sweep" split for the other three sweeps above.

_DEF_BUCKETS = (
    PriceComparableBucket(
        position_short_name="DEF", price_low=40, price_high=45, avg_early_season_points=1.0, n_observations=10
    ),
    PriceComparableBucket(
        position_short_name="DEF", price_low=46, price_high=60, avg_early_season_points=2.5, n_observations=10
    ),
)


def test_lookup_price_comparable_bucket_no_buckets_for_position_returns_none():
    assert lookup_price_comparable_bucket(_DEF_BUCKETS, "GK", price=45) is None


def test_lookup_price_comparable_bucket_empty_bucket_list_returns_none():
    assert lookup_price_comparable_bucket([], "DEF", price=45) is None


def test_lookup_price_comparable_bucket_price_below_every_bucket_extrapolates_to_lowest():
    result = lookup_price_comparable_bucket(_DEF_BUCKETS, "DEF", price=20)

    assert result is not None
    assert result.price_low == 40 and result.price_high == 45


def test_lookup_price_comparable_bucket_price_above_every_bucket_extrapolates_to_highest():
    result = lookup_price_comparable_bucket(_DEF_BUCKETS, "DEF", price=999)

    assert result is not None
    assert result.price_low == 46 and result.price_high == 60


def test_lookup_price_comparable_bucket_price_within_a_bucket_range_matches_it():
    result = lookup_price_comparable_bucket(_DEF_BUCKETS, "DEF", price=50)

    assert result is not None
    assert result.avg_early_season_points == 2.5


def test_lookup_price_comparable_bucket_price_at_exact_boundary_is_inclusive():
    # 45 is the upper (inclusive) edge of the first bucket and NOT inside the second (46-60) --
    # must resolve to the first, not silently fall through to extrapolation.
    result = lookup_price_comparable_bucket(_DEF_BUCKETS, "DEF", price=45)

    assert result is not None
    assert result.avg_early_season_points == 1.0
