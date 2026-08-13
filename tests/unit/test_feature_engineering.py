"""Unit tests for app.features.engineering: pure, DB-free feature computation.

No database, no network — every function under test takes plain
``PriorGameweekStat`` instances (or plain lists of ints) and returns a
computed value.
"""

from datetime import UTC, datetime
from decimal import Decimal

from app.features.engineering import (
    PriorGameweekStat,
    filter_to_strictly_prior,
    form_weighted_avg_points,
    home_away_avg_points,
    rest_days,
    rolling_avg_minutes,
    rolling_avg_points,
    season_avg_points_to_date,
    sum_points_for_gameweek,
)
from app.features.engineering import career_avg_early_season_points_prior_seasons as career_avg_early_season
from app.features.engineering import career_avg_points_prior_seasons as career_avg


def _dt(day: int) -> datetime:
    """A deterministic UTC datetime, day 1 = earliest."""
    return datetime(2024, 1, day, 18, 0, tzinfo=UTC)


def _stat(
    day: int,
    points: int,
    minutes: int = 90,
    was_home: bool | None = None,
    kickoff: datetime | None = None,
    season_id: str = "season-a",
    gameweek_key: str | None = None,
    fpl_event_id: int = 1,
) -> PriorGameweekStat:
    return PriorGameweekStat(
        season_id=season_id,
        gameweek_key=gameweek_key or f"gw-{day}",
        gameweek_deadline=_dt(day),
        total_points=points,
        minutes=minutes,
        was_home=was_home,
        fixture_kickoff_time=kickoff,
        fpl_event_id=fpl_event_id,
    )


# --- filter_to_strictly_prior: the most important test in this layer ------


def test_filter_to_strictly_prior_excludes_sentinel_at_target_gameweek():
    """A row belonging to the target gameweek itself must never leak into 'prior' stats."""
    target_deadline = _dt(10)
    sentinel_points = 999999
    all_stats = [
        _stat(5, points=3),
        _stat(8, points=5),
        _stat(10, points=sentinel_points),  # this IS the target gameweek — must be excluded
        _stat(12, points=sentinel_points),  # later than target — must be excluded
    ]

    prior = filter_to_strictly_prior(all_stats, target_deadline)

    assert sentinel_points not in [stat.total_points for stat in prior]
    assert len(prior) == 2
    assert {stat.total_points for stat in prior} == {3, 5}


def test_filter_to_strictly_prior_excludes_exact_boundary_equality():
    """A row whose deadline is EXACTLY equal to the target's deadline is excluded, not just later ones."""
    target_deadline = _dt(10)
    boundary_row = _stat(10, points=42)  # gameweek_deadline == target_deadline exactly

    prior = filter_to_strictly_prior([boundary_row], target_deadline)

    assert prior == []


def test_filter_to_strictly_prior_keeps_only_strictly_earlier_rows():
    target_deadline = _dt(10)
    earlier = _stat(9, points=7)
    later = _stat(11, points=8)

    prior = filter_to_strictly_prior([earlier, later], target_deadline)

    assert prior == [earlier]


# --- rolling averages -------------------------------------------------------


def test_rolling_avg_points_hand_computed_within_window():
    stats = [_stat(1, points=2), _stat(2, points=4), _stat(3, points=6)]
    assert rolling_avg_points(stats, window=3) == Decimal("4.00")


def test_rolling_avg_points_uses_most_recent_n_when_more_history_exists():
    # 5 gameweeks of history, 4/5/6/7/8 points; most recent 3 (by deadline) are 6, 7, 8.
    stats = [
        _stat(1, points=4),
        _stat(2, points=5),
        _stat(3, points=6),
        _stat(4, points=7),
        _stat(5, points=8),
    ]
    assert rolling_avg_points(stats, window=3) == Decimal("7.00")


def test_rolling_avg_points_averages_over_fewer_than_window_when_insufficient_history():
    stats = [_stat(1, points=10), _stat(2, points=20)]
    # window=5 requested but only 2 gameweeks of history exist — average over those 2.
    assert rolling_avg_points(stats, window=5) == Decimal("15.00")


def test_rolling_avg_points_returns_none_with_no_history():
    assert rolling_avg_points([], window=3) is None


def test_rolling_avg_points_skips_gameweeks_player_did_not_feature_in():
    """A gameweek with no row must be skipped, never treated as a 0."""
    # Player featured in gameweeks 1 and 3 only (gameweek 2 has no row at all —
    # never represented here, exactly as player_gameweek_stats never inserts a
    # zero-filled row for a gameweek not played).
    stats = [_stat(1, points=10), _stat(3, points=20)]
    # If gameweek 2's absence were wrongly treated as a 0, a window of 3 would
    # need 3 entries and average to 10 — instead there are only 2 real entries.
    assert rolling_avg_points(stats, window=3) == Decimal("15.00")


def test_rolling_avg_minutes_hand_computed():
    stats = [_stat(1, points=0, minutes=90), _stat(2, points=0, minutes=45), _stat(3, points=0, minutes=0)]
    assert rolling_avg_minutes(stats, window=3) == Decimal("45.00")


def test_rolling_avg_minutes_returns_none_with_no_history():
    assert rolling_avg_minutes([], window=5) is None


# --- season / career averages -----------------------------------------------


def test_season_avg_points_to_date_hand_computed():
    stats = [_stat(1, points=1), _stat(2, points=2), _stat(3, points=3), _stat(4, points=6)]
    assert season_avg_points_to_date(stats) == Decimal("3.00")


def test_season_avg_points_to_date_none_with_zero_prior_history():
    assert season_avg_points_to_date([]) is None


def test_career_avg_points_prior_seasons_hand_computed():
    stats = [
        _stat(1, points=2, season_id="season-x", gameweek_key="x-1"),
        _stat(2, points=4, season_id="season-x", gameweek_key="x-2"),
        _stat(1, points=9, season_id="season-y", gameweek_key="y-1"),
    ]
    assert career_avg(stats) == Decimal("5.00")


def test_career_avg_points_prior_seasons_none_with_no_earlier_seasons():
    assert career_avg([]) is None


# --- early-season career average --------------------------------------------


def test_career_avg_early_season_points_prior_seasons_filters_out_late_gameweeks():
    stats = [
        _stat(1, points=10, season_id="season-x", gameweek_key="x-1", fpl_event_id=1),
        _stat(2, points=20, season_id="season-x", gameweek_key="x-6", fpl_event_id=6),
        # Gameweek 7 of season-x: outside the default cutoff (6) -- must be excluded.
        _stat(3, points=999, season_id="season-x", gameweek_key="x-7", fpl_event_id=7),
    ]
    # Only the fpl_event_id<=6 rows count: (10 + 20) / 2 = 15.00.
    assert career_avg_early_season(stats) == Decimal("15.00")


def test_career_avg_early_season_points_prior_seasons_combines_across_seasons():
    """Two different seasons -- recency-weighted, not a flat average (see the dedicated
    weighting tests below): season-y (day 2) is more recent than season-x (day 1), so its
    points count for more than a plain (4+8)/2=6.00 would."""
    stats = [
        _stat(1, points=4, season_id="season-x", gameweek_key="x-1", fpl_event_id=1),
        _stat(2, points=8, season_id="season-y", gameweek_key="y-2", fpl_event_id=2),
    ]
    assert career_avg_early_season(stats) == Decimal("6.34")


def test_career_avg_early_season_points_prior_seasons_weights_within_a_season_flatly():
    """Rows sharing the SAME season get equal weight regardless of order -- only cross-season
    recency decays, never within-season recency (that's form_weighted_avg_points's own,
    separate job for within-season data)."""
    stats = [
        _stat(1, points=10, season_id="season-x", gameweek_key="x-1", fpl_event_id=1),
        _stat(2, points=20, season_id="season-x", gameweek_key="x-2", fpl_event_id=2),
    ]
    assert career_avg_early_season(stats) == Decimal("15.00")


def test_career_avg_early_season_points_prior_seasons_older_season_contributes_less():
    """A 3-season spread: the most recent season's points should pull the result closer to
    itself than an unweighted average of all three would (an unweighted average of
    100/50/0 would be 50.00 -- the recency-weighted result must be strictly higher, since
    the most recent season (100) is weighted the heaviest)."""
    stats = [
        _stat(1, points=0, season_id="season-oldest", gameweek_key="old-1", fpl_event_id=1),
        _stat(2, points=50, season_id="season-middle", gameweek_key="mid-1", fpl_event_id=1),
        _stat(3, points=100, season_id="season-newest", gameweek_key="new-1", fpl_event_id=1),
    ]
    result = career_avg_early_season(stats)
    assert result is not None
    assert result > Decimal("50.00")


def test_career_avg_early_season_points_prior_seasons_custom_half_life_widens_the_gap():
    """A shorter half_life_seasons discounts older seasons more aggressively -- the result
    should move even closer to the most recent season's own value than the default half-life
    does."""
    stats = [
        _stat(1, points=0, season_id="season-old", gameweek_key="old-1", fpl_event_id=1),
        _stat(2, points=100, season_id="season-new", gameweek_key="new-1", fpl_event_id=1),
    ]
    default_result = career_avg_early_season(stats)
    short_half_life_result = career_avg_early_season(stats, half_life_seasons=0.5)
    assert default_result is not None
    assert short_half_life_result is not None
    assert short_half_life_result > default_result


def test_career_avg_early_season_points_prior_seasons_none_with_no_earlier_seasons():
    assert career_avg_early_season([]) is None


def test_career_avg_early_season_points_prior_seasons_none_when_all_history_is_late_season():
    """A player whose only prior-season rows are all outside the early window -- e.g. every
    prior season they have a record for started with a mid-season transfer/debut -- has real
    prior-season history but none of it answers "how did they start a season", so this must
    be None, not silently 0 or fall back to the late-season rows."""
    stats = [_stat(1, points=50, season_id="season-x", gameweek_key="x-20", fpl_event_id=20)]
    assert career_avg_early_season(stats) is None


def test_career_avg_early_season_points_prior_seasons_respects_custom_cutoff():
    stats = [
        _stat(1, points=10, season_id="season-x", gameweek_key="x-4", fpl_event_id=4),
        _stat(2, points=30, season_id="season-x", gameweek_key="x-5", fpl_event_id=5),
    ]
    # cutoff=4 excludes gameweek 5, leaving only the gameweek-4 row.
    assert career_avg_early_season(stats, early_season_gameweek_cutoff=4) == Decimal("10.00")


def test_career_avg_early_season_points_prior_seasons_sums_double_gameweek_in_window():
    """Two rows sharing a gameweek_key (a Double Gameweek) inside the early window must be
    summed into one gameweek entry, exactly like career_avg_points_prior_seasons does."""
    stats = [
        _stat(1, points=5, season_id="season-x", gameweek_key="x-3-dgw", fpl_event_id=3),
        _stat(1, points=7, season_id="season-x", gameweek_key="x-3-dgw", fpl_event_id=3),
    ]
    # One gameweek entry worth 12 points -> average of a single entry is 12.00.
    assert career_avg_early_season(stats) == Decimal("12.00")


# --- form-weighted average ---------------------------------------------------


def test_form_weighted_avg_points_hand_computed_example():
    # 3 known gameweeks, most recent first: 10, 20, 30 points, half_life=1.0.
    # games_ago=0 -> weight 1.0 (points=10, the *most recent* gameweek — day 3)
    # games_ago=1 -> weight 0.5 (points=20, day 2)
    # games_ago=2 -> weight 0.25 (points=30, day 1)
    stats = [_stat(1, points=30), _stat(2, points=20), _stat(3, points=10)]
    # weighted_sum = 1.0*10 + 0.5*20 + 0.25*30 = 10 + 10 + 7.5 = 27.5
    # weight_total = 1.0 + 0.5 + 0.25 = 1.75
    # weighted_avg = 27.5 / 1.75 = 15.714285... -> rounds to 15.71
    expected = (Decimal("27.5") / Decimal("1.75")).quantize(Decimal("0.01"))
    assert form_weighted_avg_points(stats, half_life_gameweeks=1.0) == expected
    assert form_weighted_avg_points(stats, half_life_gameweeks=1.0) == Decimal("15.71")


def test_form_weighted_avg_points_none_with_no_history():
    assert form_weighted_avg_points([], half_life_gameweeks=3.0) is None


def test_form_weighted_avg_points_weighs_recent_gameweeks_higher():
    # Same set of points, but arranged so the *higher* score is more recent —
    # the weighted average should exceed the plain average in that case.
    recent_high = [_stat(1, points=10), _stat(2, points=90)]
    recent_low = [_stat(1, points=90), _stat(2, points=10)]
    plain_avg = Decimal("50.00")
    assert form_weighted_avg_points(recent_high, half_life_gameweeks=1.0) > plain_avg
    assert form_weighted_avg_points(recent_low, half_life_gameweeks=1.0) < plain_avg


# --- home / away split -------------------------------------------------------


def test_home_away_avg_points_hand_computed_mixed():
    stats = [
        _stat(1, points=2, was_home=True),
        _stat(2, points=4, was_home=True),
        _stat(3, points=9, was_home=False),
        _stat(4, points=3, was_home=False),
    ]
    home_avg, away_avg = home_away_avg_points(stats)
    assert home_avg == Decimal("3.00")
    assert away_avg == Decimal("6.00")


def test_home_away_avg_points_none_when_no_rows_for_a_venue():
    stats = [_stat(1, points=5, was_home=True)]
    home_avg, away_avg = home_away_avg_points(stats)
    assert home_avg == Decimal("5.00")
    assert away_avg is None


def test_home_away_avg_points_none_with_no_history():
    home_avg, away_avg = home_away_avg_points([])
    assert home_avg is None
    assert away_avg is None


def test_home_away_avg_points_splits_at_fixture_row_not_gameweek_level():
    """A double gameweek with one home leg and one away leg splits across both buckets, not aggregated."""
    stats = [
        _stat(1, points=6, was_home=True, gameweek_key="dgw"),
        _stat(1, points=2, was_home=False, gameweek_key="dgw"),
    ]
    home_avg, away_avg = home_away_avg_points(stats)
    assert home_avg == Decimal("6.00")
    assert away_avg == Decimal("2.00")


# --- rest days ----------------------------------------------------------------


def test_rest_days_hand_computed():
    most_recent_kickoff = datetime(2024, 3, 1, 15, 0, tzinfo=UTC)
    older_kickoff = datetime(2024, 2, 20, 15, 0, tzinfo=UTC)
    stats = [
        _stat(1, points=1, kickoff=older_kickoff),
        _stat(2, points=2, kickoff=most_recent_kickoff),
    ]
    target_kickoff = datetime(2024, 3, 8, 15, 0, tzinfo=UTC)  # 7 days after most recent
    assert rest_days(stats, target_kickoff) == 7


def test_rest_days_none_when_no_prior_kickoff_known():
    stats = [_stat(1, points=1, kickoff=None)]
    assert rest_days(stats, datetime(2024, 3, 8, tzinfo=UTC)) is None


def test_rest_days_none_with_no_history():
    assert rest_days([], datetime(2024, 3, 8, tzinfo=UTC)) is None


# --- Double Gameweek target summing (the label) ------------------------------


def test_sum_points_for_gameweek_sums_double_gameweek_fixture_rows():
    fixture_1_points = 6
    fixture_2_points = 15
    total = sum_points_for_gameweek([fixture_1_points, fixture_2_points])
    assert total == 21
    assert total != fixture_1_points
    assert total != fixture_2_points


def test_sum_points_for_gameweek_single_fixture():
    assert sum_points_for_gameweek([9]) == 9


def test_sum_points_for_gameweek_empty_sums_to_zero():
    assert sum_points_for_gameweek([]) == 0


# --- Null-handling: unrelated schema-drift-null fields never affect calculations ---


def test_unrelated_none_fields_do_not_break_points_or_minutes_calculations():
    stats = [
        PriorGameweekStat(
            season_id="season-a",
            gameweek_key="gw-1",
            gameweek_deadline=_dt(1),
            total_points=5,
            minutes=90,
            was_home=True,
            fixture_kickoff_time=None,
            fpl_event_id=1,
            expected_goals=None,  # simulates a schema-drift-absent column (pre-2021-22 seasons)
        ),
        PriorGameweekStat(
            season_id="season-a",
            gameweek_key="gw-2",
            gameweek_deadline=_dt(2),
            total_points=7,
            minutes=90,
            was_home=False,
            fixture_kickoff_time=None,
            fpl_event_id=2,
            expected_goals=Decimal("0.42"),  # present for a later-era season
        ),
    ]
    assert rolling_avg_points(stats, window=2) == Decimal("6.00")
    assert rolling_avg_minutes(stats, window=2) == Decimal("90.00")
    assert season_avg_points_to_date(stats) == Decimal("6.00")
    home_avg, away_avg = home_away_avg_points(stats)
    assert home_avg == Decimal("5.00")
    assert away_avg == Decimal("7.00")
