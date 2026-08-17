"""Pure, DB-free feature engineering functions for Milestone 3 Layer 2.

Every function here takes plain in-memory data (``PriorGameweekStat``
instances, or plain lists of ints) and returns a computed value. No
SQLAlchemy session, no I/O, no ORM types — this is what makes the module
fully unit-testable in isolation and lets the leakage-prevention guarantee be
checked structurally rather than only by convention.

The leakage rule, precisely
-----------------------------
A feature row being computed for target gameweek ``G`` may only use
``player_gameweek_stats`` rows whose own gameweek's ``deadline_time`` is
STRICTLY EARLIER than ``G``'s ``deadline_time`` — never ``G`` itself (even
though ``G`` hasn't technically happened yet at the moment of the deadline,
its own deadline is the boundary, not a data point), and never anything with
a later deadline.

Every "prior stats" function below takes a ``prior_stats`` parameter that the
CALLER must have already filtered to be strictly prior — the function
signatures themselves don't accept a raw cutoff, specifically so it's
structurally hard to accidentally include the boundary row via an off-by-one
in some other call site. ``filter_to_strictly_prior`` is the one function
whose entire job is to perform that filtering correctly; every caller
(service-layer code, tests) should go through it rather than reimplementing
the comparison inline.

Double Gameweek aggregation
------------------------------
A player can have more than one ``player_gameweek_stats`` row for the same
gameweek (a Double Gameweek — see migration 0008). For every "per-gameweek"
statistic below (rolling/season/career/form-weighted averages of points or
minutes) rows sharing the same gameweek are summed together first, so a
Double Gameweek counts as one (higher-scoring) gameweek entry rather than two
separate ones — this mirrors exactly how ``target_total_points`` (the label)
itself is computed via ``sum_points_for_gameweek``, keeping the feature
computation consistent with what it's trying to predict.

The home/away split is the one exception: venue is a property of the
individual fixture, not the gameweek as a whole (a double gameweek can mix a
home leg and an away leg), so ``home_away_avg_points`` splits at the
individual-row level instead.

Null handling
--------------
``player_gameweek_stats.total_points``/``minutes``/``goals_scored``/``assists``
are never-null columns in the database (defaulted to ``0``), so the
corresponding ``PriorGameweekStat`` fields are always plain ``int``.
``expected_goals``/``expected_assists``/``defensive_contribution`` are real,
schema-drift-affected columns (verified against the real ingested data: xG/xA
are ``NULL`` for every 2020-21/2021-22 row and fully populated from 2022-23
onward; ``defensive_contribution`` is ``NULL`` for every season before
2025-26 — the FPL season that stat's own scoring rule was introduced in, and
fully populated for 2025-26). ``rolling_goal_involvement_overperformance``
and ``rolling_defensive_contribution`` are the only functions here that read
these fields; every other function remains indifferent to them (see the unit
tests demonstrating that carrying these fields alongside real data never
perturbs a points/minutes-based calculation). Both new functions skip a
gameweek missing the relevant field entirely rather than treating it as a
false zero — the same "unknown, not zero" convention as everywhere else in
this module.

A gameweek a player did NOT feature in has no ``player_gameweek_stats`` row
at all (never a zero-filled row) — so it is simply absent from
``prior_stats`` and every averaging function below correctly skips it,
rather than diluting an average with a false zero.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

__all__ = [
    "PriorGameweekStat",
    "filter_to_strictly_prior",
    "rolling_avg_points",
    "rolling_avg_minutes",
    "season_avg_points_to_date",
    "career_avg_points_prior_seasons",
    "career_avg_early_season_points_prior_seasons",
    "form_weighted_avg_points",
    "home_away_avg_points",
    "rest_days",
    "sum_points_for_gameweek",
    "rolling_goal_involvement_overperformance",
    "rolling_defensive_contribution",
]

# Default early-season window for career_avg_early_season_points_prior_seasons:
# matches app.jobs.handlers.squad_optimization.SQUAD_BUILD_HORIZON_LENGTH (6) --
# the same "how far into a fresh season are we projecting" horizon already used
# for cold-start squad building, so a player's own early-season history is
# measured over the same span the projection is actually being asked about.
DEFAULT_EARLY_SEASON_GAMEWEEK_CUTOFF = 6

# Half-life, in SEASONS (not gameweeks), for career_avg_early_season_points_prior_seasons'
# cross-season recency weighting: a season 2 seasons ago is weighted half as much as the
# most recent one available, 4 seasons ago a quarter, etc. Deliberately much coarser than
# FORM_HALF_LIFE_GAMEWEEKS (3 gameweeks, in app/features/service.py) -- seasons are a much
# larger unit of "how much has this player's game changed", and with at most ~6 seasons of
# real history to draw on (HISTORICAL_DATA_INGESTION's EARLIEST_USABLE_SEASON), a half-life
# this short would nearly zero out everything but the last 1-2 seasons; 2.0 keeps recent
# seasons dominant while still letting older ones meaningfully contribute, rather than
# ignoring them outright (a flat, unweighted average was the prior behavior, which gave a
# season 6 years ago exactly as much say as last season -- the gap this constant closes).
DEFAULT_EARLY_SEASON_HALF_LIFE_SEASONS = 2.0


@dataclass(frozen=True)
class PriorGameweekStat:
    """One player_gameweek_stats row's worth of data, reduced to plain values.

    Assembled by the service layer (``app/features/service.py``) from
    ``PlayerGameweekStats``/``Gameweek``/``Fixture`` query results — never an
    ORM row itself, so this module stays free of any DB dependency.

    ``season_id`` is a plain string (e.g. ``str(uuid.UUID(...))``), carried
    so the service layer can partition a combined, already-filtered list
    back into "this season" vs. "an earlier season" after applying
    ``filter_to_strictly_prior`` once across both. Also read by
    ``career_avg_early_season_points_prior_seasons`` to rank each row's own
    season by recency (most-recent-season-present = 0 "seasons ago") for
    its exponential recency weighting -- every other function in this
    module remains indifferent to it.

    ``gameweek_key`` identifies which gameweek this row belongs to (e.g.
    ``str(gameweek_id)``) — distinct from ``gameweek_deadline``, which is
    used purely for chronological ordering/filtering. Two rows sharing the
    same ``gameweek_key`` (a Double Gameweek) are summed together by the
    per-gameweek averaging functions below.

    ``fpl_event_id`` is the gameweek's number WITHIN its own season (1-38,
    FPL's own numbering) — distinct from ``gameweek_deadline`` (an absolute
    timestamp, useless for asking "was this row within the first N
    gameweeks of ITS season", since a season's own gameweek 1 has a
    different absolute date every year). Only
    ``career_avg_early_season_points_prior_seasons`` reads this field today;
    every other function below is indifferent to it.

    ``expected_goals``/``expected_assists``/``defensive_contribution`` are only
    read by ``rolling_goal_involvement_overperformance``/
    ``rolling_defensive_contribution`` — see the module docstring's "Null
    handling" section. ``goals_scored``/``assists`` are only read by
    ``rolling_goal_involvement_overperformance``; every other function here
    remains indifferent to all five fields.
    """

    season_id: str
    gameweek_key: str
    gameweek_deadline: datetime
    total_points: int
    minutes: int
    was_home: bool | None
    fixture_kickoff_time: datetime | None
    fpl_event_id: int
    expected_goals: Decimal | None = None
    goals_scored: int = 0
    assists: int = 0
    expected_assists: Decimal | None = None
    defensive_contribution: int | None = None


@dataclass(frozen=True)
class _GameweekAggregate:
    gameweek_key: str
    season_id: str
    deadline: datetime
    total_points: int
    minutes: int
    goals_scored: int = 0
    assists: int = 0
    # None means "at least one contributing row lacked this field" (schema
    # drift, or -- for a Double Gameweek -- one leg missing it while the
    # other has it) -- poisoning the whole gameweek's aggregate to unknown
    # rather than silently summing only the rows that happened to have data,
    # which would understate a real value. See _sum_optional_decimal/_int.
    expected_goals: Decimal | None = None
    expected_assists: Decimal | None = None
    defensive_contribution: int | None = None


def _sum_optional_decimal(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    """``None`` if either operand is ``None`` -- see ``_GameweekAggregate``'s own comment."""
    if a is None or b is None:
        return None
    return a + b


def _sum_optional_int(a: int | None, b: int | None) -> int | None:
    """``None`` if either operand is ``None`` -- see ``_GameweekAggregate``'s own comment."""
    if a is None or b is None:
        return None
    return a + b


def filter_to_strictly_prior(
    all_stats: Sequence[PriorGameweekStat], target_deadline: datetime
) -> list[PriorGameweekStat]:
    """The single place the leakage-prevention boundary is enforced.

    Keeps only rows whose gameweek deadline is STRICTLY EARLIER than
    ``target_deadline`` — a row whose deadline is exactly equal to
    ``target_deadline`` (i.e. it belongs to the target gameweek itself) is
    excluded, not just rows with a later deadline.
    """
    return [stat for stat in all_stats if stat.gameweek_deadline < target_deadline]


def _round2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _aggregate_by_gameweek(prior_stats: Sequence[PriorGameweekStat]) -> list[_GameweekAggregate]:
    """Group rows sharing a ``gameweek_key`` (a Double Gameweek) into one summed entry per gameweek.

    Returned most-recent-first (descending by deadline) — every caller that
    needs a "most recent N gameweeks" window relies on this ordering.
    """
    groups: dict[str, _GameweekAggregate] = {}
    for stat in prior_stats:
        existing = groups.get(stat.gameweek_key)
        if existing is None:
            groups[stat.gameweek_key] = _GameweekAggregate(
                gameweek_key=stat.gameweek_key,
                season_id=stat.season_id,
                deadline=stat.gameweek_deadline,
                total_points=stat.total_points,
                minutes=stat.minutes,
                goals_scored=stat.goals_scored,
                assists=stat.assists,
                expected_goals=stat.expected_goals,
                expected_assists=stat.expected_assists,
                defensive_contribution=stat.defensive_contribution,
            )
        else:
            groups[stat.gameweek_key] = _GameweekAggregate(
                gameweek_key=existing.gameweek_key,
                season_id=existing.season_id,
                deadline=existing.deadline,
                total_points=existing.total_points + stat.total_points,
                minutes=existing.minutes + stat.minutes,
                goals_scored=existing.goals_scored + stat.goals_scored,
                assists=existing.assists + stat.assists,
                expected_goals=_sum_optional_decimal(existing.expected_goals, stat.expected_goals),
                expected_assists=_sum_optional_decimal(existing.expected_assists, stat.expected_assists),
                defensive_contribution=_sum_optional_int(
                    existing.defensive_contribution, stat.defensive_contribution
                ),
            )
    return sorted(groups.values(), key=lambda agg: agg.deadline, reverse=True)


def rolling_avg_points(prior_stats: Sequence[PriorGameweekStat], window: int) -> Decimal | None:
    """Average ``total_points`` over the most recent ``window`` gameweeks with a row.

    A gameweek the player didn't feature in has no row and is never treated
    as a ``0``. Averages over however many gameweeks of history actually
    exist if fewer than ``window`` are available. Returns ``None`` if there
    is no prior history at all.
    """
    aggregates = _aggregate_by_gameweek(prior_stats)
    if not aggregates:
        return None
    recent = aggregates[:window]
    return _round2(Decimal(sum(agg.total_points for agg in recent)) / Decimal(len(recent)))


def rolling_avg_minutes(prior_stats: Sequence[PriorGameweekStat], window: int) -> Decimal | None:
    """Same pattern as ``rolling_avg_points``, over ``minutes`` instead."""
    aggregates = _aggregate_by_gameweek(prior_stats)
    if not aggregates:
        return None
    recent = aggregates[:window]
    return _round2(Decimal(sum(agg.minutes for agg in recent)) / Decimal(len(recent)))


def season_avg_points_to_date(prior_stats_this_season_only: Sequence[PriorGameweekStat]) -> Decimal | None:
    """Average points per gameweek across all prior gameweeks of the current season.

    ``None`` if there is no prior history at all this season.
    """
    aggregates = _aggregate_by_gameweek(prior_stats_this_season_only)
    if not aggregates:
        return None
    return _round2(Decimal(sum(agg.total_points for agg in aggregates)) / Decimal(len(aggregates)))


def career_avg_points_prior_seasons(
    prior_stats_from_earlier_seasons_only: Sequence[PriorGameweekStat],
) -> Decimal | None:
    """Average points per gameweek across every earlier season's prior gameweeks.

    Cross-season resolution (matching rows across seasons via ``fpl_code``)
    is the caller's job (see ``app/features/service.py``) — this function
    just averages whatever rows it's given, identically to
    ``season_avg_points_to_date``. ``None`` if given no rows (a player with
    no earlier-season history — including a player in only their first FPL
    season ever).
    """
    aggregates = _aggregate_by_gameweek(prior_stats_from_earlier_seasons_only)
    if not aggregates:
        return None
    return _round2(Decimal(sum(agg.total_points for agg in aggregates)) / Decimal(len(aggregates)))


def career_avg_early_season_points_prior_seasons(
    prior_stats_from_earlier_seasons_only: Sequence[PriorGameweekStat],
    early_season_gameweek_cutoff: int = DEFAULT_EARLY_SEASON_GAMEWEEK_CUTOFF,
    half_life_seasons: float = DEFAULT_EARLY_SEASON_HALF_LIFE_SEASONS,
) -> Decimal | None:
    """Recency-weighted average points per gameweek across only the EARLY gameweeks
    (``fpl_event_id <= early_season_gameweek_cutoff``) of every earlier season this player
    has a record for.

    This exists because ``career_avg_points_prior_seasons`` (a full-season average) is the
    wrong number for projecting a fresh season's own opening gameweeks: a player's September
    form and their April form get blended into one number that represents neither. Some
    players are well-established slow starters (need match fitness/rhythm) or fast starters
    (thrive on a pre-season rebuild) relative to their OWN season-long average — a real,
    observable pattern this function is specifically built to surface, using whichever prior
    seasons' early gameweeks are actually available (via ``fpl_code`` cross-season resolution,
    exactly like ``career_avg_points_prior_seasons`` — see that function's own docstring for the
    resolution contract, identical here).

    Recency-weighted ACROSS SEASONS (not a flat average of every early-season row, which was
    this function's original behavior — see ``DEFAULT_EARLY_SEASON_HALF_LIFE_SEASONS``'s own
    comment for why an unweighted average is the wrong default): each row is weighted by
    ``0.5 ** (seasons_ago / half_life_seasons)``, where ``seasons_ago`` is that row's own
    season's rank among the DISTINCT seasons actually present in this player's data (0 = the
    most recent season present, 1 = one season before that, ...) — not a fixed calendar
    distance, so a player missing a season's early gameweeks entirely (e.g. a mid-season
    debut) doesn't skew everyone else's ranking. All rows within the same season share that
    season's single weight; only cross-season recency decays, exactly mirroring
    ``form_weighted_avg_points``'s existing within-season, per-gameweek decay pattern one
    level up (seasons instead of gameweeks).

    ``None`` if the player has no rows falling inside the early-season window in ANY prior
    season — covers both a genuinely new player (no prior-season history at all) and the
    narrower case of a player whose prior-season history exists but never falls early in a
    season (e.g. every one of their FPL seasons started with a mid-season transfer/debut, so
    they were never present for gameweek 1-``early_season_gameweek_cutoff`` of any of them).
    Both are real "we don't know this player's early-season form" cases, not a `0`.
    """
    early_stats = [
        stat
        for stat in prior_stats_from_earlier_seasons_only
        if stat.fpl_event_id <= early_season_gameweek_cutoff
    ]
    aggregates = _aggregate_by_gameweek(early_stats)
    if not aggregates:
        return None

    # Rank each DISTINCT season by recency (most-recent-season-present's own latest
    # deadline first) -- aggregates is already sorted most-recent-gameweek-first, so the
    # first time a season_id is seen is that season's own most recent early-window row.
    season_recency_rank: dict[str, int] = {}
    for agg in aggregates:
        if agg.season_id not in season_recency_rank:
            season_recency_rank[agg.season_id] = len(season_recency_rank)

    weighted_sum = Decimal("0")
    weight_total = Decimal("0")
    for agg in aggregates:
        seasons_ago = season_recency_rank[agg.season_id]
        weight = Decimal(str(0.5 ** (seasons_ago / half_life_seasons)))
        weighted_sum += weight * Decimal(agg.total_points)
        weight_total += weight

    return _round2(weighted_sum / weight_total)


def form_weighted_avg_points(
    prior_stats: Sequence[PriorGameweekStat], half_life_gameweeks: float
) -> Decimal | None:
    """Exponentially recency-weighted average of points per gameweek.

    ``weight = 0.5 ** (games_ago / half_life_gameweeks)`` where the most
    recent prior gameweek has ``games_ago = 0`` (full weight) and each
    gameweek further back is weighted less, halving every
    ``half_life_gameweeks`` gameweeks. The result is the weighted average:
    ``sum(weight_i * points_i) / sum(weight_i)``. ``None`` if there's no
    prior history at all.
    """
    aggregates = _aggregate_by_gameweek(prior_stats)
    if not aggregates:
        return None

    weighted_sum = Decimal("0")
    weight_total = Decimal("0")
    for games_ago, agg in enumerate(aggregates):
        weight = Decimal(str(0.5 ** (games_ago / half_life_gameweeks)))
        weighted_sum += weight * Decimal(agg.total_points)
        weight_total += weight

    return _round2(weighted_sum / weight_total)


def home_away_avg_points(
    prior_stats: Sequence[PriorGameweekStat],
) -> tuple[Decimal | None, Decimal | None]:
    """Average ``total_points`` split by venue, at the individual fixture-row level.

    Unlike the per-gameweek averages above, this does NOT aggregate a Double
    Gameweek's two rows into one entry first — venue (``was_home``) is a
    property of the individual fixture, and a double gameweek can mix a home
    leg and an away leg. Returns ``(home_avg, away_avg)``, each independently
    ``None`` if there are no rows for that venue.
    """
    home_points = [stat.total_points for stat in prior_stats if stat.was_home is True]
    away_points = [stat.total_points for stat in prior_stats if stat.was_home is False]
    home_avg = _round2(Decimal(sum(home_points)) / Decimal(len(home_points))) if home_points else None
    away_avg = _round2(Decimal(sum(away_points)) / Decimal(len(away_points))) if away_points else None
    return home_avg, away_avg


def rest_days(prior_stats: Sequence[PriorGameweekStat], target_fixture_kickoff_time: datetime) -> int | None:
    """Days between the player's most recent prior fixture kickoff and ``target_fixture_kickoff_time``.

    ``None`` if no prior row carries a known fixture kickoff time (e.g. no
    prior history, or every prior row's fixture couldn't be resolved).
    """
    kickoffs = [stat.fixture_kickoff_time for stat in prior_stats if stat.fixture_kickoff_time is not None]
    if not kickoffs:
        return None
    most_recent = max(kickoffs)
    return (target_fixture_kickoff_time - most_recent).days


def sum_points_for_gameweek(stats_for_that_gameweek: Sequence[int]) -> int:
    """Sum ``total_points`` across every fixture row a player has for one (target) gameweek.

    Correctly handles a Double Gameweek by summing rather than picking one
    row — this is the label (``target_total_points``) computation. An empty
    sequence (the player had zero fixture rows that gameweek) sums to ``0``,
    matching FPL's own convention that a player who didn't feature simply
    scores no points that gameweek (as distinct from a gameweek that hasn't
    been played yet at all, which the caller represents as ``None`` rather
    than calling this function).
    """
    return sum(stats_for_that_gameweek)


def rolling_goal_involvement_overperformance(
    prior_stats: Sequence[PriorGameweekStat], window: int
) -> Decimal | None:
    """Average ``(goals + assists) - (expected_goals + expected_assists)`` over the most recent
    ``window`` gameweeks that actually carry expected-goals data.

    A standard regression-to-mean signal absent from every other function in this module (all of
    which are points/minutes-history only): a player scoring well above their own cumulative xG+xA
    is running hot and likely to cool off; one well below it is "due" a correction. This is a
    genuinely different signal from points history alone — it distinguishes "output has really
    improved" from "getting lucky/unlucky with finishing", which a points-only rolling average
    cannot.

    Gameweeks missing ``expected_goals``/``expected_assists`` (schema drift — ``NULL`` for every
    2020-21/2021-22 row, see the module docstring) are excluded from the window entirely, not
    treated as a false zero — so ``window`` counts gameweeks that actually have xG data, not
    gameweeks in general (a player with a long history but only recent xG coverage still gets a
    real, unskewed answer using just the gameweeks that have it). ``None`` if no prior gameweek
    carries xG data at all.
    """
    aggregates = _aggregate_by_gameweek(prior_stats)
    gameweeks_with_xg = [
        agg for agg in aggregates if agg.expected_goals is not None and agg.expected_assists is not None
    ]
    if not gameweeks_with_xg:
        return None
    recent = gameweeks_with_xg[:window]
    total = Decimal("0")
    for agg in recent:
        assert agg.expected_goals is not None
        assert agg.expected_assists is not None
        actual = Decimal(agg.goals_scored + agg.assists)
        expected = agg.expected_goals + agg.expected_assists
        total += actual - expected
    return _round2(total / Decimal(len(recent)))


def rolling_defensive_contribution(prior_stats: Sequence[PriorGameweekStat], window: int) -> Decimal | None:
    """Average ``defensive_contribution`` over the most recent ``window`` gameweeks that carry it.

    ``defensive_contribution`` is FPL's own real, current-season scoring stat (the 2025-26+ CBIT
    threshold bonus: defenders earn 2 points for reaching a per-match clearances+blocks+
    interceptions threshold, midfielders/forwards for the higher CBIT threshold) — not just an
    external analytics stat, but part of the actual points formula the model is trying to predict.
    Neither ``total_points`` history nor any other feature here breaks defensive actions out
    separately, so without this, a player trending toward the threshold is invisible to every
    existing feature until it shows up as points after the fact.

    Schema-drift-affected exactly like ``expected_goals`` (see the module docstring): ``NULL`` for
    every season before 2025-26 (verified against real ingested data — this stat didn't exist
    before that rule was introduced), fully populated for 2025-26. Gameweeks missing it are
    excluded from the window entirely, never treated as a false zero. ``None`` if no prior
    gameweek carries this stat at all.
    """
    aggregates = _aggregate_by_gameweek(prior_stats)
    gameweeks_with_dc = [agg for agg in aggregates if agg.defensive_contribution is not None]
    if not gameweeks_with_dc:
        return None
    recent = gameweeks_with_dc[:window]
    total = 0
    for agg in recent:
        assert agg.defensive_contribution is not None
        total += agg.defensive_contribution
    return _round2(Decimal(total) / Decimal(len(recent)))
