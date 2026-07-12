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
``player_gameweek_stats.total_points``/``minutes`` are never-null columns in
the database (defaulted to ``0``), so ``PriorGameweekStat.total_points``/
``minutes`` are always plain ``int``. Other, schema-drift-affected columns
(``expected_goals`` and similar) are irrelevant to every calculation in this
module; ``PriorGameweekStat.expected_goals`` exists only to demonstrate (see
the unit tests) that carrying such a field alongside real data never
perturbs a points/minutes-based calculation.

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
    "form_weighted_avg_points",
    "home_away_avg_points",
    "rest_days",
    "sum_points_for_gameweek",
]


@dataclass(frozen=True)
class PriorGameweekStat:
    """One player_gameweek_stats row's worth of data, reduced to plain values.

    Assembled by the service layer (``app/features/service.py``) from
    ``PlayerGameweekStats``/``Gameweek``/``Fixture`` query results — never an
    ORM row itself, so this module stays free of any DB dependency.

    ``season_id`` is a plain string (e.g. ``str(uuid.UUID(...))``), carried
    only so the service layer can partition a combined, already-filtered
    list back into "this season" vs. "an earlier season" after applying
    ``filter_to_strictly_prior`` once across both; it is never interpreted or
    compared by any function in this module.

    ``gameweek_key`` identifies which gameweek this row belongs to (e.g.
    ``str(gameweek_id)``) — distinct from ``gameweek_deadline``, which is
    used purely for chronological ordering/filtering. Two rows sharing the
    same ``gameweek_key`` (a Double Gameweek) are summed together by the
    per-gameweek averaging functions below.

    ``expected_goals`` is never read by any function here — see the module
    docstring's "Null handling" section.
    """

    season_id: str
    gameweek_key: str
    gameweek_deadline: datetime
    total_points: int
    minutes: int
    was_home: bool | None
    fixture_kickoff_time: datetime | None
    expected_goals: Decimal | None = None


@dataclass(frozen=True)
class _GameweekAggregate:
    gameweek_key: str
    deadline: datetime
    total_points: int
    minutes: int


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
                deadline=stat.gameweek_deadline,
                total_points=stat.total_points,
                minutes=stat.minutes,
            )
        else:
            groups[stat.gameweek_key] = _GameweekAggregate(
                gameweek_key=existing.gameweek_key,
                deadline=existing.deadline,
                total_points=existing.total_points + stat.total_points,
                minutes=existing.minutes + stat.minutes,
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
