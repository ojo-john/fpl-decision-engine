"""Historical validation for ``app.optimization.projection._cold_start_projection``'s hand-written
blend formula -- built because ``BACKTEST`` (``app.jobs.handlers.backtest``) and
``MODEL_TRAINING``'s own walk-forward validation (``app.modeling.walk_forward``) ONLY ever evaluate
the trained ML model (a real ``model_versions`` row); ``_cold_start_projection`` is a completely
separate formula that runs whenever ``_is_cold_start_season`` is true (every pre-season squad-build
decision this app exists to support) and had NEVER been checked against real historical outcomes
before this module existed.

Three constants are validated here, each by re-using the SAME leakage-safe, per-(player, season)
observation set built once by ``build_player_season_observations`` -- deliberately one harness, not
three, per the same "reuse the real production function, never reimplement it" discipline this
module applies to ``app.features.engineering.career_avg_early_season_points_prior_seasons`` itself:

1. ``app.features.engineering.DEFAULT_EARLY_SEASON_HALF_LIFE_SEASONS`` -- swept via
   ``sweep_half_life_candidates``.
2. The fixture-difficulty adjustment coefficient in ``_cold_start_projection``
   (``1 + (3 - difficulty) * 0.075``) -- swept via ``sweep_difficulty_coefficient_candidates``,
   at per-gameweek granularity (the one dimension that genuinely varies within a player's own
   early-season window) using REAL historical fixture difficulty ratings and REAL per-gameweek
   actual points -- no ``ep_next`` dependency at all, so this sweep carries none of the caveat below.
3. The ep_next/career_avg blend ratio (currently a flat 50/50 average) -- swept via
   ``sweep_blend_ratio_candidates``, with an unavoidable, explicitly documented caveat: ``ep_next``
   has no true historical PRE-SEASON snapshot (see that function's own docstring for what was
   checked and why the available proxy is a materially different, weaker signal than what the live
   formula actually blends with).

A fourth, structurally different piece of validation lives here too: ``build_true_rookie_observations``
/ ``compute_price_comparable_buckets`` / ``sweep_price_prior_blend_weight_candidates`` validate a
price-informed fallback for the population ``build_player_season_observations`` above deliberately
EXCLUDES -- players with LITERALLY ZERO strictly-prior-season history (``prior_stats`` empty, not
just missing an EARLY-season window). ``_cold_start_projection``'s ``career_avg is None`` branch
returns ``project_player``'s own ``ep_next``-first estimate completely untouched for this population
today -- the single least-informed group of players currently gets the LEAST scrutiny, not more. See
each function's own docstring for the leakage design and an important, EXTRA caveat beyond the
ep_next-blend one above (see ``sweep_price_prior_blend_weight_candidates``).

Leakage discipline
-------------------
Every prediction for target season S is computed using ONLY strictly-prior-season data, enforced by
reusing the real ``app.features.engineering.filter_to_strictly_prior`` against season S's own
earliest gameweek deadline -- the same function/discipline the production feature pipeline uses,
not a reimplementation of it. See ``build_player_season_observations``.
"""

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.features import engineering
from app.features.engineering import PriorGameweekStat
from app.modeling import metrics as metric_fns
from packages.database.models.fixture import Fixture
from packages.database.models.gameweek import Gameweek
from packages.database.models.player import Player
from packages.database.models.player_gameweek_stats import PlayerGameweekStats
from packages.database.models.season import Season

__all__ = [
    "EARLY_SEASON_HALF_LIFE_SWEEP_CANDIDATES",
    "FLAT_CONTROL_HALF_LIFE_SEASONS",
    "DIFFICULTY_COEFFICIENT_SWEEP_CANDIDATES",
    "BLEND_RATIO_SWEEP_CANDIDATES",
    "PRICE_COMPARABLE_BUCKETS_PER_POSITION",
    "PRICE_PRIOR_BLEND_WEIGHT_SWEEP_CANDIDATES",
    "DC_CBIT_THRESHOLD_BY_POSITION",
    "DC_RELIABILITY_COEFFICIENT_SWEEP_CANDIDATES",
    "PlayerSeasonObservation",
    "EarlyGameweekActual",
    "HalfLifeSweepResult",
    "DifficultyCoefficientSweepResult",
    "BlendRatioSweepResult",
    "TrueRookieObservation",
    "PriceComparablePoolEntry",
    "PriceComparableBucket",
    "PricePriorSweepResult",
    "DcReliabilityObservation",
    "DcReliabilityCoefficientSweepResult",
    "usable_season_labels_with_gameweek_stats",
    "build_player_season_observations",
    "sweep_half_life_candidates",
    "sweep_difficulty_coefficient_candidates",
    "sweep_blend_ratio_candidates",
    "build_true_rookie_observations",
    "compute_price_comparable_buckets",
    "sweep_price_prior_blend_weight_candidates",
    "lookup_price_comparable_bucket",
    "build_dc_reliability_observations",
    "sweep_dc_reliability_coefficient_candidates",
    "defensive_contribution_hit_rate_prior_seasons",
]

# A season 2 seasons ago weighted half as much as the most recent one, etc. -- see
# app.features.engineering's own module-level comment for the full rationale; this is the exact
# candidate set the Joao Pedro finding motivated re-checking (half-life 2.0's blend materially
# under-weighted his clear, monotonic season-over-season improvement).
EARLY_SEASON_HALF_LIFE_SWEEP_CANDIDATES: tuple[float, ...] = (0.5, 0.75, 1.0, 1.5, 2.0, 3.0)

# The "flat, no recency weighting" control: mathematically equivalent to an unweighted mean since
# 0.5 ** (seasons_ago / 1_000_000) rounds to 1.0 for any realistic seasons_ago (at most ~5, given
# HISTORICAL_DATA_INGESTION's 6-season depth) -- a real call into the real, unmodified
# career_avg_early_season_points_prior_seasons, not a second, hand-rolled "flat average" code path.
FLAT_CONTROL_HALF_LIFE_SEASONS = 1_000_000.0

# The fixture-difficulty adjustment's own multiplier coefficient -- see _cold_start_projection's
# docstring: "±15% across the full 1-5 range, neutral at difficulty 3" is 0.075 today. 0.0 is an
# explicit no-adjustment control.
DIFFICULTY_COEFFICIENT_SWEEP_CANDIDATES: tuple[float, ...] = (0.0, 0.025, 0.05, 0.075, 0.10, 0.15)

# Weight on career_avg in the ep_next/career_avg blend; ep_next gets (1 - weight). 0.5 is today's
# flat average. See sweep_blend_ratio_candidates's own docstring for the ep_next proxy caveat.
BLEND_RATIO_SWEEP_CANDIDATES: tuple[float, ...] = (0.3, 0.4, 0.5, 0.6, 0.7)

# Quantile buckets per position for the price-comparable lookup table -- pools every player (any
# prior-history status) with resolvable early-season data, not just true rookies (their own sample
# across 6 real seasons is too small, tens not hundreds, to bucket reliably on its own). 5 (quintiles)
# balances real per-bucket sample size against granularity; a position with fewer real entries than
# this collapses to fewer, larger buckets automatically (see _bucket_pool_entries) rather than ever
# producing an unreliable, near-empty bucket.
PRICE_COMPARABLE_BUCKETS_PER_POSITION = 5

# Weight on the price-comparable-bucket estimate in the price-prior/ep_next blend (ep_next gets
# 1 - weight); 0.0 is today's production behavior (career_avg is None -> baseline untouched). See
# sweep_price_prior_blend_weight_candidates's own docstring for why this is a WEAKER evidentiary
# footing than BLEND_RATIO_SWEEP_CANDIDATES's own already-caveated ep_next proxy.
PRICE_PRIOR_BLEND_WEIGHT_SWEEP_CANDIDATES: tuple[float, ...] = (0.0, 0.3, 0.5, 0.7, 1.0)

# Real FPL 2025-26+ defensive-contribution bonus thresholds (2 points, capped at 2 per match,
# never more). Verified 2026-08-18 against real, current sources (the Premier League's own "What's
# new for 2025/26" announcement and Draft Fantasy's 2026/27 DEFCON explainer) rather than assumed:
# defenders need 10 combined clearances+blocks+interceptions+tackles (CBIT) in a match; midfielders
# (and forwards) need a HIGHER 12, because their own count also includes ball recoveries (CBIRT) --
# a real, position-dependent asymmetry, not one universal number. This module only ever computes
# this signal for DEF/MID (see ``build_dc_reliability_observations``) -- FWD/GKP aren't where this
# rule is meaningfully concentrated in practice (see that function's own docstring).
DC_CBIT_THRESHOLD_BY_POSITION: dict[str, int] = {"DEF": 10, "MID": 12}

# Candidate coefficients for a "trust this player's aggregate historical average more when it's
# demonstrably driven by a repeatable defensive-work-rate skill" adjustment -- see
# sweep_dc_reliability_coefficient_candidates's own docstring for the exact formula, and, critically,
# why real evaluation of this sweep currently returns ZERO observations for every single candidate
# (defensive_contribution is only non-null for the 2025-26 season -- the CBIT rule's first real
# season -- and leakage-safe validation needs a player's STRICTLY PRIOR season's own DC data to
# predict a LATER, already-known season's outcome; 2025-26 has no strictly-prior season carrying
# this stat, and the next season that would (2026-27) hasn't been played yet). This sweep is real,
# correct, and ready -- it just has nothing to validate against yet. See this module's own top-level
# docstring and ARCHITECTURE.md's cold-start-validation section for the honest, current state.
DC_RELIABILITY_COEFFICIENT_SWEEP_CANDIDATES: tuple[float, ...] = (0.0, 0.05, 0.1, 0.15, 0.2, 0.3)


@dataclass(frozen=True)
class EarlyGameweekActual:
    """One real, already-played early-season (``fpl_event_id <= cutoff``) gameweek row for a
    player -- the per-gameweek granularity ``sweep_difficulty_coefficient_candidates`` needs, since
    ``next_fixture_difficulty`` (unlike career_avg or ep_next) genuinely varies gameweek-to-gameweek.
    One entry per ``(player, gameweek, fixture)`` row, mirroring ``player_gameweek_stats``'s own
    Double-Gameweek-aware key -- a DGW's two fixtures are evaluated as two independent observations
    (both against the same season-level baseline prediction) rather than merged, since there is no
    single well-defined "difficulty" for two fixtures at once.
    """

    fpl_event_id: int
    actual_points: int
    difficulty: int


@dataclass(frozen=True)
class PlayerSeasonObservation:
    """One real (player, target season) evaluation pair: what the cold-start formula's
    career_avg-based component WOULD have predicted for this player's early-season form going into
    ``season_label``, using only strictly-prior-season data, versus what actually happened.
    """

    fpl_code: int
    season_label: str
    actual_early_season_avg: float
    prior_stats: tuple[PriorGameweekStat, ...]
    early_gameweek_actuals: tuple[EarlyGameweekActual, ...]
    # Most recent strictly-prior season's end-of-that-season ep_next snapshot for this player, or
    # None if unavailable (new-to-data-in-that-season player, or that season's row never got a
    # non-null ep_next) -- see sweep_blend_ratio_candidates's own docstring for why this is a
    # materially imperfect proxy, used only for that one sweep.
    ep_next_proxy: float | None = None


@dataclass(frozen=True)
class HalfLifeSweepResult:
    half_life_seasons: float
    label: str
    # None only if this candidate ended up with zero scoreable pairs -- genuinely undefined, not 0
    # (mirrors app.modeling.metrics.spearman_rank_correlation's own "None means undefined" contract).
    mae: float | None
    spearman: float | None
    n_observations: int


@dataclass(frozen=True)
class DifficultyCoefficientSweepResult:
    coefficient: float
    mae: float | None
    spearman: float | None
    n_observations: int


@dataclass(frozen=True)
class BlendRatioSweepResult:
    career_avg_weight: float
    ep_next_weight: float
    mae: float | None
    spearman: float | None
    n_observations: int


@dataclass(frozen=True)
class TrueRookieObservation:
    """One real (player, target season) evaluation pair for a player with LITERALLY ZERO
    strictly-prior-season ``player_gameweek_stats`` rows -- the exact population
    ``_cold_start_projection``'s ``career_avg is None`` branch serves, and which
    ``build_player_season_observations`` deliberately EXCLUDES (it requires non-empty
    ``prior_stats``; a genuinely new player has none). See ``build_true_rookie_observations``.
    """

    fpl_code: int
    season_label: str
    actual_early_season_avg: float
    # This player's price/position as of their EARLIEST early-season gameweek row in the TARGET
    # season itself (there is, by definition, no prior-season row to draw either from) -- the
    # closest real, non-leaky proxy for "price at the point a genuinely new player would first
    # need a projection". See _load_earliest_early_gameweek_price_and_position's own docstring.
    price_at_prediction_time: int
    position_short_name: str
    # The TARGET season's own ep_next snapshot for this player, or None -- see
    # sweep_price_prior_blend_weight_candidates's own docstring for why this (not a prior
    # season's ep_next, which cannot exist for a true rookie) is the only available proxy, and
    # why it is MORE leaky than BLEND_RATIO_SWEEP_CANDIDATES's own already-caveated one.
    ep_next_same_season_proxy: float | None = None


@dataclass(frozen=True)
class PriceComparablePoolEntry:
    """One real (position, price, actual_early_season_avg) triple from a usable season, for ANY
    player with resolvable early-season data -- the raw material ``_bucket_pool_entries`` groups
    into buckets. Deliberately not scoped to true rookies alone: a market sets a new signing's
    price using the full population of comparably-priced/comparably-rated players as its
    reference point, not just other rookies, and true rookies alone are far too rare across 6 real
    seasons to bucket reliably on their own (see ``PRICE_COMPARABLE_BUCKETS_PER_POSITION``).
    """

    season_label: str
    position_short_name: str
    price: int
    actual_early_season_avg: float


@dataclass(frozen=True)
class PriceComparableBucket:
    """One real (position, price range) -> average early-season output bucket. ``price_low``/
    ``price_high`` are inclusive and drawn from the real entries in the bucket (not evenly-spaced
    synthetic cutoffs) -- see ``_bucket_pool_entries``."""

    position_short_name: str
    price_low: int
    price_high: int
    avg_early_season_points: float
    n_observations: int


@dataclass(frozen=True)
class PricePriorSweepResult:
    price_prior_weight: float
    ep_next_weight: float
    mae: float | None
    spearman: float | None
    n_observations: int


@dataclass(frozen=True)
class DcReliabilityObservation:
    """One real, leakage-safe (player, target season) evaluation pair for the DC-reliability
    adjustment -- DEF/MID only (see ``DC_CBIT_THRESHOLD_BY_POSITION``), requiring at least one real
    ``defensive_contribution``-carrying gameweek in STRICTLY PRIOR seasons.

    ``career_avg_baseline`` is the SAME isolated ``career_avg_early_season_points_prior_seasons``
    component ``sweep_difficulty_coefficient_candidates`` evaluates against -- deliberately not the
    full ``ep_next``-blended prediction, so this sweep carries none of
    ``sweep_blend_ratio_candidates``'s own ``ep_next``-proxy caveat either.
    """

    fpl_code: int
    season_label: str
    position_short_name: str
    actual_early_season_avg: float
    career_avg_baseline: float
    prior_dc_hit_rate: float
    prior_dc_games: int


@dataclass(frozen=True)
class DcReliabilityCoefficientSweepResult:
    coefficient: float
    mae: float | None
    spearman: float | None
    n_observations: int


def _candidate_label(half_life_seasons: float) -> str:
    return (
        "flat_no_weighting"
        if half_life_seasons == FLAT_CONTROL_HALF_LIFE_SEASONS
        else f"{half_life_seasons:g}"
    )


def usable_season_labels_with_gameweek_stats(session: Session) -> list[str]:
    """Every season label, chronologically sorted, with >=1 real ``player_gameweek_stats`` row.

    Mirrors ``app.modeling.walk_forward.usable_season_labels_with_features``'s own shape/rationale
    (determined at runtime via a DB query, never hardcoded; season labels sort correctly as plain
    strings) but keys off raw ``player_gameweek_stats`` existence rather than ``player_features``
    rows -- this module reads historical stats directly and has no dependency on
    ``FEATURE_GENERATION`` having been run first.
    """
    labels = (
        session.execute(
            select(Season.label)
            .join(PlayerGameweekStats, PlayerGameweekStats.season_id == Season.id)
            .distinct()
        )
        .scalars()
        .all()
    )
    return sorted(labels)


def _load_all_stats_by_fpl_code(session: Session) -> dict[int, list[PriorGameweekStat]]:
    """One bulk query for every non-quarantined player's full career of stats, keyed by ``fpl_code``
    -- mirrors ``app.features.service.generate_features_for_season``'s own "preload once, compute in
    memory" discipline (avoids O(seasons x players) round trips), just spanning every season instead
    of one.
    """
    rows = session.execute(
        select(
            Player.fpl_code,
            Player.season_id,
            PlayerGameweekStats.gameweek_id,
            PlayerGameweekStats.total_points,
            Gameweek.deadline_time.label("gw_deadline"),
            Gameweek.fpl_event_id,
        )
        .join(PlayerGameweekStats, PlayerGameweekStats.player_id == Player.id)
        .join(Gameweek, Gameweek.id == PlayerGameweekStats.gameweek_id)
        .where(Player.fpl_code.is_not(None), Player.is_quarantined.is_(False))
    ).all()

    stats_by_fpl_code: dict[int, list[PriorGameweekStat]] = defaultdict(list)
    for row in rows:
        stats_by_fpl_code[row.fpl_code].append(
            PriorGameweekStat(
                season_id=str(row.season_id),
                gameweek_key=str(row.gameweek_id),
                gameweek_deadline=row.gw_deadline,
                total_points=row.total_points,
                minutes=0,
                was_home=None,
                fixture_kickoff_time=None,
                fpl_event_id=row.fpl_event_id,
            )
        )
    return stats_by_fpl_code


def _earliest_gameweek_deadline_by_season(
    session: Session, season_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, datetime]:
    rows = session.execute(
        select(Gameweek.season_id, func.min(Gameweek.deadline_time))
        .where(Gameweek.season_id.in_(season_ids))
        .group_by(Gameweek.season_id)
    ).all()
    return {season_id: deadline for season_id, deadline in rows}


def _load_early_gameweek_fixture_rows(
    session: Session, season_ids: Sequence[uuid.UUID], early_season_gameweek_cutoff: int
) -> dict[tuple[int, uuid.UUID], list[EarlyGameweekActual]]:
    """Real per-fixture early-season (``fpl_event_id <= cutoff``) rows for the given seasons only --
    the ``next_fixture_difficulty`` sweep's own data, kept separate from ``_load_all_stats_by_fpl_code``
    (which spans every season but carries no fixture/difficulty info at all).

    Skips rows with no resolvable fixture or ``was_home`` (mirrors ``_cold_start_projection``'s own
    ``if next_fixture_difficulty is not None`` guard -- a gameweek this formula couldn't have adjusted
    for live either contributes nothing here).
    """
    if not season_ids:
        return {}
    rows = session.execute(
        select(
            Player.fpl_code,
            Player.season_id,
            PlayerGameweekStats.total_points,
            PlayerGameweekStats.was_home,
            Gameweek.fpl_event_id,
            Fixture.home_difficulty,
            Fixture.away_difficulty,
        )
        .join(PlayerGameweekStats, PlayerGameweekStats.player_id == Player.id)
        .join(Gameweek, Gameweek.id == PlayerGameweekStats.gameweek_id)
        .join(Fixture, Fixture.id == PlayerGameweekStats.fixture_id)
        .where(
            Player.fpl_code.is_not(None),
            Player.is_quarantined.is_(False),
            Player.season_id.in_(season_ids),
            Gameweek.fpl_event_id <= early_season_gameweek_cutoff,
        )
    ).all()

    result: dict[tuple[int, uuid.UUID], list[EarlyGameweekActual]] = defaultdict(list)
    for row in rows:
        if row.was_home is None:
            continue
        difficulty = row.home_difficulty if row.was_home else row.away_difficulty
        if difficulty is None:
            continue
        result[(row.fpl_code, row.season_id)].append(
            EarlyGameweekActual(
                fpl_event_id=row.fpl_event_id, actual_points=row.total_points, difficulty=difficulty
            )
        )
    return result


def _load_ep_next_by_fpl_code_and_season(session: Session) -> dict[tuple[int, str], float]:
    """Every non-null, non-quarantined ``players.ep_next`` value, keyed by ``(fpl_code, season_label)``
    -- see ``sweep_blend_ratio_candidates``'s own docstring for the vintage caveat on what this value
    actually represents historically.
    """
    rows = session.execute(
        select(Player.fpl_code, Season.label, Player.ep_next)
        .join(Season, Season.id == Player.season_id)
        .where(Player.fpl_code.is_not(None), Player.is_quarantined.is_(False), Player.ep_next.is_not(None))
    ).all()
    return {(fpl_code, season_label): float(ep_next) for fpl_code, season_label, ep_next in rows}


def _most_recent_prior_ep_next(
    ep_next_by_key: dict[tuple[int, str], float],
    fpl_code: int,
    usable_seasons: Sequence[str],
    season_label: str,
) -> float | None:
    target_index = usable_seasons.index(season_label)
    for prior_label in reversed(usable_seasons[:target_index]):
        value = ep_next_by_key.get((fpl_code, prior_label))
        if value is not None:
            return value
    return None


def _load_earliest_early_gameweek_price_and_position(
    session: Session, season_ids: Sequence[uuid.UUID], early_season_gameweek_cutoff: int
) -> dict[tuple[int, uuid.UUID], tuple[int, str]]:
    """This player's (price, position) as of their EARLIEST early-season gameweek row (lowest
    ``fpl_event_id <= cutoff``) in the given seasons -- mirrors
    ``app.features.service.compute_features_for_player_gameweek``'s own documented precedent for
    exactly the "player has no strictly-prior row" case: "a player's price ... going into a
    gameweek's deadline is public, pre-deadline-knowable information ... not leakage", so using
    THIS season's own earliest resolvable row is the correct, real, non-leaky signal -- not a
    stale prior-season value (which cannot exist for a true rookie) and not ``players.now_cost``
    (a season-END snapshot for historical seasons, per ``sweep_blend_ratio_candidates``'s own
    documented caveat on the identical underlying data-vintage problem for ``ep_next``).

    Skips rows with a null ``value_at_time``/``position_short_name_at_time`` (real,
    schema-drift-affected nullable columns -- see ``PlayerGameweekStats``'s own module docstring)
    rather than ever falling back to a zero/None value silently.
    """
    if not season_ids:
        return {}
    rows = session.execute(
        select(
            Player.fpl_code,
            Player.season_id,
            Gameweek.fpl_event_id,
            PlayerGameweekStats.value_at_time,
            PlayerGameweekStats.position_short_name_at_time,
        )
        .join(PlayerGameweekStats, PlayerGameweekStats.player_id == Player.id)
        .join(Gameweek, Gameweek.id == PlayerGameweekStats.gameweek_id)
        .where(
            Player.fpl_code.is_not(None),
            Player.is_quarantined.is_(False),
            Player.season_id.in_(season_ids),
            Gameweek.fpl_event_id <= early_season_gameweek_cutoff,
            PlayerGameweekStats.value_at_time.is_not(None),
            PlayerGameweekStats.position_short_name_at_time.is_not(None),
        )
    ).all()

    earliest: dict[tuple[int, uuid.UUID], tuple[int, int, str]] = {}
    for row in rows:
        key = (row.fpl_code, row.season_id)
        existing = earliest.get(key)
        if existing is None or row.fpl_event_id < existing[0]:
            earliest[key] = (row.fpl_event_id, row.value_at_time, row.position_short_name_at_time)
    return {key: (value, position) for key, (_, value, position) in earliest.items()}


def _price_comparable_pool_entries(
    season_labels: Sequence[str],
    season_id_by_label: dict[str, uuid.UUID],
    stats_by_fpl_code: dict[int, list[PriorGameweekStat]],
    price_position_by_key: dict[tuple[int, uuid.UUID], tuple[int, str]],
    early_season_gameweek_cutoff: int,
) -> list[PriceComparablePoolEntry]:
    """Real (position, price, actual_early_season_avg) triples for every player with resolvable
    early-season data across ``season_labels`` -- the shared building block both
    ``compute_price_comparable_buckets`` (the LIVE artifact, given every usable season) and
    ``sweep_price_prior_blend_weight_candidates`` (given only a target season's STRICTLY PRIOR
    seasons, per eval season -- see that function's own leakage handling) call with a different
    ``season_labels`` slice.
    """
    entries: list[PriceComparablePoolEntry] = []
    for season_label in season_labels:
        season_id = season_id_by_label[season_label]
        season_id_str = str(season_id)
        for fpl_code, all_stats in stats_by_fpl_code.items():
            current_season_stats = [stat for stat in all_stats if stat.season_id == season_id_str]
            if not current_season_stats:
                continue
            actual = engineering.career_avg_early_season_points_prior_seasons(
                current_season_stats, early_season_gameweek_cutoff, half_life_seasons=1.0
            )
            if actual is None:
                continue
            price_position = price_position_by_key.get((fpl_code, season_id))
            if price_position is None:
                continue
            price, position_short_name = price_position
            entries.append(
                PriceComparablePoolEntry(
                    season_label=season_label,
                    position_short_name=position_short_name,
                    price=price,
                    actual_early_season_avg=float(actual),
                )
            )
    return entries


def _bucket_pool_entries(
    entries: Sequence[PriceComparablePoolEntry], n_buckets_per_position: int
) -> list[PriceComparableBucket]:
    """Groups entries by position, sorts by price, and splits into up to ``n_buckets_per_position``
    equal-sized quantile buckets per position, each with a real average early-season output.

    A position with fewer real entries than ``n_buckets_per_position`` collapses to fewer, larger
    buckets (never divides into more buckets than there is real data to fill -- correctness over a
    fixed bucket count) via ``min(n_buckets_per_position, n)``. A position with zero entries
    produces zero buckets (not a synthetic/zero-filled one).
    """
    by_position: dict[str, list[PriceComparablePoolEntry]] = defaultdict(list)
    for entry in entries:
        by_position[entry.position_short_name].append(entry)

    buckets: list[PriceComparableBucket] = []
    for position, position_entries in by_position.items():
        sorted_entries = sorted(position_entries, key=lambda e: e.price)
        n = len(sorted_entries)
        effective_buckets = min(n_buckets_per_position, n)
        if effective_buckets == 0:
            continue
        chunk_size = n / effective_buckets
        for bucket_index in range(effective_buckets):
            start = round(bucket_index * chunk_size)
            end = round((bucket_index + 1) * chunk_size) if bucket_index < effective_buckets - 1 else n
            chunk = sorted_entries[start:end]
            if not chunk:
                continue
            buckets.append(
                PriceComparableBucket(
                    position_short_name=position,
                    price_low=chunk[0].price,
                    price_high=chunk[-1].price,
                    avg_early_season_points=sum(e.actual_early_season_avg for e in chunk) / len(chunk),
                    n_observations=len(chunk),
                )
            )
    return buckets


def lookup_price_comparable_bucket(
    buckets: Sequence[PriceComparableBucket], position_short_name: str, price: int
) -> PriceComparableBucket | None:
    """The bucket for ``position_short_name`` whose ``[price_low, price_high]`` range contains
    ``price``, or the nearest edge bucket if ``price`` falls outside every observed bucket's real
    range (extrapolation to the closest known bucket rather than ``None`` -- a price above/below
    every historical comparable is still closer to the nearest bucket's real average than to
    nothing). ``None`` only if this position has no buckets at all.

    Public (not module-private) so ``app.optimization.projection``'s live prediction path can
    reuse the exact same lookup logic the sweep below uses to score candidates, rather than a
    second, potentially-diverging reimplementation.
    """
    position_buckets = [b for b in buckets if b.position_short_name == position_short_name]
    if not position_buckets:
        return None
    for bucket in position_buckets:
        if bucket.price_low <= price <= bucket.price_high:
            return bucket
    position_buckets_sorted = sorted(position_buckets, key=lambda b: b.price_low)
    if price < position_buckets_sorted[0].price_low:
        return position_buckets_sorted[0]
    return position_buckets_sorted[-1]


def compute_price_comparable_buckets(
    session: Session,
    n_buckets_per_position: int = PRICE_COMPARABLE_BUCKETS_PER_POSITION,
    early_season_gameweek_cutoff: int = engineering.DEFAULT_EARLY_SEASON_GAMEWEEK_CUTOFF,
) -> tuple[list[PriceComparableBucket], list[str]]:
    """The LIVE price-comparable lookup table -- persisted to
    ``cold_start_validation_runs.price_comparable_buckets_json`` and consulted by
    ``app.optimization.projection._cold_start_projection`` for a genuinely new player.

    Deliberately NOT leakage-scoped to a single held-out season the way
    ``sweep_price_prior_blend_weight_candidates`` below is: this is the artifact used to predict a
    FUTURE, not-yet-played season, so it legitimately pools every real historical season available
    -- the exact same "train on everything available, predict forward" principle the trained
    champion model itself already follows (``MODEL_TRAINING`` is not leakage-scoped against the
    live/future season either, only against its own walk-forward validation folds).
    """
    usable = usable_season_labels_with_gameweek_stats(session)
    if not usable:
        return [], usable
    season_rows = session.execute(select(Season.id, Season.label).where(Season.label.in_(usable))).all()
    season_id_by_label = {label: season_id for season_id, label in season_rows}
    stats_by_fpl_code = _load_all_stats_by_fpl_code(session)
    price_position_by_key = _load_earliest_early_gameweek_price_and_position(
        session, list(season_id_by_label.values()), early_season_gameweek_cutoff
    )
    entries = _price_comparable_pool_entries(
        usable, season_id_by_label, stats_by_fpl_code, price_position_by_key, early_season_gameweek_cutoff
    )
    return _bucket_pool_entries(entries, n_buckets_per_position), usable


def build_true_rookie_observations(
    session: Session, early_season_gameweek_cutoff: int = engineering.DEFAULT_EARLY_SEASON_GAMEWEEK_CUTOFF
) -> tuple[list[TrueRookieObservation], list[str]]:
    """Every real, leakage-safe (player, target season) observation for the TRUE-ROOKIE
    population -- players with literally zero strictly-prior-season ``player_gameweek_stats``
    rows -- across every usable historical season. The mirror-image filter of
    ``build_player_season_observations`` (which requires non-empty ``prior_stats``): this function
    requires EMPTY ``prior_stats``.

    A season contributes as an evaluation target only from the second usable season onward, same
    boundary case as ``build_player_season_observations``. Skipped (no observation) if
    ``prior_stats`` is non-empty (not a true rookie -- a different, already-covered population),
    if the target season's own early-window average is ``None`` (no real GW1-6 data that season),
    or if no real price/position is resolvable for this player's earliest early-season row (see
    ``_load_earliest_early_gameweek_price_and_position``).
    """
    usable = usable_season_labels_with_gameweek_stats(session)
    if len(usable) < 2:
        return [], usable

    season_rows = session.execute(select(Season.id, Season.label).where(Season.label.in_(usable))).all()
    season_id_by_label = {label: season_id for season_id, label in season_rows}
    eval_labels = usable[1:]
    eval_season_ids = [season_id_by_label[label] for label in eval_labels]

    gw1_deadline_by_season_id = _earliest_gameweek_deadline_by_season(
        session, list(season_id_by_label.values())
    )
    stats_by_fpl_code = _load_all_stats_by_fpl_code(session)
    price_position_by_key = _load_earliest_early_gameweek_price_and_position(
        session, eval_season_ids, early_season_gameweek_cutoff
    )
    ep_next_by_key = _load_ep_next_by_fpl_code_and_season(session)

    observations: list[TrueRookieObservation] = []
    for season_label in eval_labels:
        season_id = season_id_by_label[season_label]
        season_id_str = str(season_id)
        gw1_deadline = gw1_deadline_by_season_id[season_id]

        for fpl_code, all_stats in stats_by_fpl_code.items():
            current_season_stats = [stat for stat in all_stats if stat.season_id == season_id_str]
            if not current_season_stats:
                continue
            actual = engineering.career_avg_early_season_points_prior_seasons(
                current_season_stats, early_season_gameweek_cutoff, half_life_seasons=1.0
            )
            if actual is None:
                continue

            prior_stats = engineering.filter_to_strictly_prior(all_stats, gw1_deadline)
            if prior_stats:
                continue  # has SOME prior-season history -- a different, already-covered population

            price_position = price_position_by_key.get((fpl_code, season_id))
            if price_position is None:
                continue
            price, position_short_name = price_position

            observations.append(
                TrueRookieObservation(
                    fpl_code=fpl_code,
                    season_label=season_label,
                    actual_early_season_avg=float(actual),
                    price_at_prediction_time=price,
                    position_short_name=position_short_name,
                    ep_next_same_season_proxy=ep_next_by_key.get((fpl_code, season_label)),
                )
            )
    return observations, usable


def sweep_price_prior_blend_weight_candidates(
    session: Session,
    true_rookie_observations: Sequence[TrueRookieObservation],
    usable_seasons: Sequence[str],
    candidates: Sequence[float] = PRICE_PRIOR_BLEND_WEIGHT_SWEEP_CANDIDATES,
    n_buckets_per_position: int = PRICE_COMPARABLE_BUCKETS_PER_POSITION,
    early_season_gameweek_cutoff: int = engineering.DEFAULT_EARLY_SEASON_GAMEWEEK_CUTOFF,
) -> list[PricePriorSweepResult]:
    """Real MAE/Spearman for the price-comparable-bucket estimate blended with an ep_next proxy,
    per candidate price-prior weight (``price_prior_weight=0.0`` is today's production behavior:
    ``career_avg is None`` -> ``baseline`` untouched; ``1.0`` is the price-comparable estimate
    alone).

    Leakage handling for the comparable-bucket table itself: unlike ``compute_price_comparable_buckets``
    (the live artifact, which legitimately pools every usable season), evaluating target season S
    here uses buckets built ONLY from seasons strictly before S in ``usable_seasons`` -- one bucket
    table per eval season, rebuilt fresh each time, exactly mirroring
    ``build_player_season_observations``'s own per-season leakage discipline.

    IMPORTANT, WORSE-than-``sweep_blend_ratio_candidates`` caveat on the ep_next side of this
    blend: that function's own proxy (a prior season's END-OF-SEASON ep_next) is unavailable here
    by construction -- a true rookie has no prior season at all. The only real, resolvable proxy is
    ``obs.ep_next_same_season_proxy``, the TARGET season's own end-of-season ep_next snapshot --
    meaning the ``price_prior_weight=0.0`` control in this sweep is being compared against a value
    that already reflects most of a season's hindsight for that same player, not a genuine
    pre-season estimate. This makes ``weight=0.0``'s own MAE here OPTIMISTIC/unrealistic, which
    could make a price-based prior look relatively WORSE than it truly would against FPL's actual,
    genuinely pre-season live ep_next. A result showing the price prior beating even this
    optimistic proxy is strong, conservative evidence in its favor; a result showing ep_next alone
    "winning" should be treated with real skepticism given this bias, not taken at face value.
    """
    season_rows = session.execute(
        select(Season.id, Season.label).where(Season.label.in_(usable_seasons))
    ).all()
    season_id_by_label = {label: season_id for season_id, label in season_rows}
    stats_by_fpl_code = _load_all_stats_by_fpl_code(session)
    price_position_by_key = _load_earliest_early_gameweek_price_and_position(
        session, list(season_id_by_label.values()), early_season_gameweek_cutoff
    )

    buckets_by_eval_season: dict[str, list[PriceComparableBucket]] = {}
    for index, season_label in enumerate(usable_seasons):
        prior_labels = usable_seasons[:index]
        if not prior_labels:
            continue
        entries = _price_comparable_pool_entries(
            prior_labels,
            season_id_by_label,
            stats_by_fpl_code,
            price_position_by_key,
            early_season_gameweek_cutoff,
        )
        buckets_by_eval_season[season_label] = _bucket_pool_entries(entries, n_buckets_per_position)

    pairs: list[tuple[float, float, float]] = []  # (comparable_avg, ep_next_proxy, actual)
    for obs in true_rookie_observations:
        if obs.ep_next_same_season_proxy is None:
            continue
        buckets = buckets_by_eval_season.get(obs.season_label, [])
        bucket = lookup_price_comparable_bucket(
            buckets, obs.position_short_name, obs.price_at_prediction_time
        )
        if bucket is None:
            continue
        pairs.append(
            (bucket.avg_early_season_points, obs.ep_next_same_season_proxy, obs.actual_early_season_avg)
        )

    results = []
    for price_prior_weight in candidates:
        ep_next_weight = 1 - price_prior_weight
        y_true = [actual for _, _, actual in pairs]
        y_pred = [
            price_prior_weight * comparable_avg + ep_next_weight * ep_next
            for comparable_avg, ep_next, _ in pairs
        ]
        results.append(
            PricePriorSweepResult(
                price_prior_weight=price_prior_weight,
                ep_next_weight=ep_next_weight,
                mae=metric_fns.mean_absolute_error(y_true, y_pred) if pairs else None,
                spearman=metric_fns.spearman_rank_correlation(y_true, y_pred) if pairs else None,
                n_observations=len(pairs),
            )
        )
    return results


def build_player_season_observations(
    session: Session, early_season_gameweek_cutoff: int = engineering.DEFAULT_EARLY_SEASON_GAMEWEEK_CUTOFF
) -> tuple[list[PlayerSeasonObservation], list[str]]:
    """Build every real, leakage-safe (player, target season) observation evaluable across every
    usable historical season -- the one shared harness every sweep in this module reuses.

    A season contributes as an EVALUATION TARGET only from the second usable season onward (the
    earliest usable season has no strictly-prior season to predict from at all -- a real boundary
    case, not an oversight); it still contributes as PRIOR data for every later season. For each
    target season S and each non-quarantined player with an ``fpl_code``:

    - ``prior_stats`` = ``filter_to_strictly_prior`` (the real production leakage guard) applied to
      this player's ENTIRE cross-season history, cut off at season S's own earliest gameweek
      deadline -- provably excludes every row from season S itself and any later season, since
      football seasons never overlap in calendar time.
    - Skipped (no observation) if ``prior_stats`` is empty (no prior season at all -- the
      "genuinely new player" boundary case), if the real
      ``career_avg_early_season_points_prior_seasons`` returns ``None`` for ``prior_stats`` (prior
      seasons exist but none fell in the early window -- the other documented ``None`` case that
      function's own docstring calls out), or if season S's own early-window average is ``None``
      (player has no real GW1-6 data that season at all).

    Returns ``(observations, usable_season_labels)`` -- callers that need to fail cleanly on "not
    enough data" check ``len(usable_season_labels) < 2`` themselves (see
    ``app.jobs.handlers.cold_start_projection_validation``).
    """
    usable = usable_season_labels_with_gameweek_stats(session)
    if len(usable) < 2:
        return [], usable

    season_rows = session.execute(select(Season.id, Season.label).where(Season.label.in_(usable))).all()
    season_id_by_label = {label: season_id for season_id, label in season_rows}
    eval_labels = usable[1:]
    eval_season_ids = [season_id_by_label[label] for label in eval_labels]

    gw1_deadline_by_season_id = _earliest_gameweek_deadline_by_season(
        session, list(season_id_by_label.values())
    )
    stats_by_fpl_code = _load_all_stats_by_fpl_code(session)
    early_gw_rows_by_key = _load_early_gameweek_fixture_rows(
        session, eval_season_ids, early_season_gameweek_cutoff
    )
    ep_next_by_key = _load_ep_next_by_fpl_code_and_season(session)

    observations: list[PlayerSeasonObservation] = []
    for season_label in eval_labels:
        season_id = season_id_by_label[season_label]
        season_id_str = str(season_id)
        gw1_deadline = gw1_deadline_by_season_id[season_id]

        for fpl_code, all_stats in stats_by_fpl_code.items():
            current_season_stats = [stat for stat in all_stats if stat.season_id == season_id_str]
            if not current_season_stats:
                continue
            # half_life_seasons is irrelevant here: current_season_stats all share one season_id, so
            # every aggregate gets season_recency_rank 0 and weight 1 regardless of the value passed
            # -- this is a real call to the real function computing a genuine unweighted average of
            # THIS season's own early gameweeks, not a reimplementation of that averaging logic.
            actual = engineering.career_avg_early_season_points_prior_seasons(
                current_season_stats, early_season_gameweek_cutoff, half_life_seasons=1.0
            )
            if actual is None:
                continue

            prior_stats = engineering.filter_to_strictly_prior(all_stats, gw1_deadline)
            if not prior_stats:
                continue
            # Probe with an arbitrary candidate: whether the real function returns None here depends
            # only on early_season_gameweek_cutoff filtering (season-independent of half_life), so
            # this determines "has any early-window prior data" once, for every sweep, consistently.
            probe = engineering.career_avg_early_season_points_prior_seasons(
                prior_stats, early_season_gameweek_cutoff, half_life_seasons=1.0
            )
            if probe is None:
                continue

            observations.append(
                PlayerSeasonObservation(
                    fpl_code=fpl_code,
                    season_label=season_label,
                    actual_early_season_avg=float(actual),
                    prior_stats=tuple(prior_stats),
                    early_gameweek_actuals=tuple(early_gw_rows_by_key.get((fpl_code, season_id), [])),
                    ep_next_proxy=_most_recent_prior_ep_next(ep_next_by_key, fpl_code, usable, season_label),
                )
            )
    return observations, usable


def sweep_half_life_candidates(
    observations: Sequence[PlayerSeasonObservation],
    candidates: Sequence[float] = EARLY_SEASON_HALF_LIFE_SWEEP_CANDIDATES,
    early_season_gameweek_cutoff: int = engineering.DEFAULT_EARLY_SEASON_GAMEWEEK_CUTOFF,
) -> list[HalfLifeSweepResult]:
    """Real MAE/Spearman for ``career_avg_early_season_points_prior_seasons`` alone, per candidate
    half-life, aggregated across every observation -- isolated from ``ep_next`` entirely (see module
    docstring: half-life only ever affects this one component of the blend).
    """
    results = []
    for candidate in candidates:
        y_true = [obs.actual_early_season_avg for obs in observations]
        y_pred = []
        for obs in observations:
            predicted = engineering.career_avg_early_season_points_prior_seasons(
                obs.prior_stats, early_season_gameweek_cutoff, candidate
            )
            # Invariant established by build_player_season_observations: every observation's
            # prior_stats was already probed to produce a real prediction, and that probe's
            # None-ness doesn't depend on which half-life candidate is used (see that function's
            # own comment) -- so this is never actually None here.
            assert predicted is not None
            y_pred.append(float(predicted))
        results.append(
            HalfLifeSweepResult(
                half_life_seasons=candidate,
                label=_candidate_label(candidate),
                mae=metric_fns.mean_absolute_error(y_true, y_pred) if y_true else None,
                spearman=metric_fns.spearman_rank_correlation(y_true, y_pred) if y_true else None,
                n_observations=len(y_true),
            )
        )
    return results


def sweep_difficulty_coefficient_candidates(
    observations: Sequence[PlayerSeasonObservation],
    candidates: Sequence[float] = DIFFICULTY_COEFFICIENT_SWEEP_CANDIDATES,
    half_life_seasons: float = engineering.DEFAULT_EARLY_SEASON_HALF_LIFE_SEASONS,
    early_season_gameweek_cutoff: int = engineering.DEFAULT_EARLY_SEASON_GAMEWEEK_CUTOFF,
) -> list[DifficultyCoefficientSweepResult]:
    """Real MAE/Spearman for ``_cold_start_projection``'s fixture-difficulty multiplier
    (``1 + (3 - difficulty) * coefficient``), per candidate coefficient, at real per-gameweek
    granularity -- the one dimension of the formula that genuinely varies within a player's own
    early season.

    Deliberately no ``ep_next`` involvement at all: the baseline being adjusted here is the same
    isolated ``career_avg_early_season_points_prior_seasons`` component the half-life sweep uses
    (held fixed at ``half_life_seasons``, the current production default unless overridden), so this
    sweep's real MAE numbers carry none of ``sweep_blend_ratio_candidates``'s historical-snapshot
    caveat -- ``next_fixture_difficulty`` is a real, non-leaky, per-fixture rating known well before
    that gameweek's deadline in both the live and historical data (``fixtures.csv``'s own
    ``team_h_difficulty``/``team_a_difficulty`` columns), unlike ``ep_next``.
    """
    pairs: list[tuple[float, int, int]] = []  # (baseline, difficulty, actual_points)
    for obs in observations:
        baseline = engineering.career_avg_early_season_points_prior_seasons(
            obs.prior_stats, early_season_gameweek_cutoff, half_life_seasons
        )
        if baseline is None:
            continue
        for gw in obs.early_gameweek_actuals:
            pairs.append((float(baseline), gw.difficulty, gw.actual_points))

    results = []
    for coefficient in candidates:
        y_true = [actual for _, _, actual in pairs]
        y_pred = [baseline * (1 + (3 - difficulty) * coefficient) for baseline, difficulty, _ in pairs]
        results.append(
            DifficultyCoefficientSweepResult(
                coefficient=coefficient,
                mae=metric_fns.mean_absolute_error(y_true, y_pred) if pairs else None,
                spearman=metric_fns.spearman_rank_correlation(y_true, y_pred) if pairs else None,
                n_observations=len(pairs),
            )
        )
    return results


def sweep_blend_ratio_candidates(
    observations: Sequence[PlayerSeasonObservation],
    candidates: Sequence[float] = BLEND_RATIO_SWEEP_CANDIDATES,
    half_life_seasons: float = engineering.DEFAULT_EARLY_SEASON_HALF_LIFE_SEASONS,
    early_season_gameweek_cutoff: int = engineering.DEFAULT_EARLY_SEASON_GAMEWEEK_CUTOFF,
) -> list[BlendRatioSweepResult]:
    """Real MAE/Spearman for the ep_next/career_avg blend ratio, per candidate career_avg weight
    (``ep_next`` gets ``1 - weight``) -- with an unavoidable data-vintage caveat this module's
    author verified directly, not assumed: ``ep_next`` has NO per-gameweek historical snapshot at
    all (``merged_gw.csv``, the per-gameweek historical source, has no ``ep_next`` column --
    confirmed via the committed fixture's own header). It exists only on the season-scoped
    ``players`` row, sourced from ``players_raw.csv``, which is a SINGLE snapshot taken at whatever
    point that season's data was last scraped (in practice, at or near that season's own end) --
    i.e. it reflects FPL's live algorithm's estimate near the END of the season the player already
    played, not a pre-season estimate informed by that summer's real transfers/injuries/manager
    changes, which is what the LIVE cold-start formula actually blends with.

    Used here as ``obs.ep_next_proxy`` (the most recent strictly-prior season's own end-of-season
    ``ep_next``) for real, executable candidate MAE numbers per the requested sweep, but this
    function's own result should NOT be treated as direct evidence for changing the production blend
    ratio -- a proxy this stale could make ``ep_next`` look artificially unhelpful (or artificially
    helpful) relative to what genuinely current, transfer-aware ``ep_next`` contributes live. See the
    calling job's own persisted ``blend_ratio_caveat`` field and this repo's cold-start-validation
    ARCHITECTURE.md section for how this caveat is surfaced to any reader of a real run's results.
    """
    pairs: list[tuple[float, float, float]] = []  # (career_avg, ep_next_proxy, actual)
    for obs in observations:
        if obs.ep_next_proxy is None:
            continue
        career_avg = engineering.career_avg_early_season_points_prior_seasons(
            obs.prior_stats, early_season_gameweek_cutoff, half_life_seasons
        )
        if career_avg is None:
            continue
        pairs.append((float(career_avg), obs.ep_next_proxy, obs.actual_early_season_avg))

    results = []
    for career_avg_weight in candidates:
        ep_next_weight = 1 - career_avg_weight
        y_true = [actual for _, _, actual in pairs]
        y_pred = [
            career_avg_weight * career_avg + ep_next_weight * ep_next for career_avg, ep_next, _ in pairs
        ]
        results.append(
            BlendRatioSweepResult(
                career_avg_weight=career_avg_weight,
                ep_next_weight=ep_next_weight,
                mae=metric_fns.mean_absolute_error(y_true, y_pred) if pairs else None,
                spearman=metric_fns.spearman_rank_correlation(y_true, y_pred) if pairs else None,
                n_observations=len(pairs),
            )
        )
    return results


def _load_dc_rows_by_fpl_code(session: Session) -> dict[int, list[tuple[uuid.UUID, datetime, int, str]]]:
    """Every real, non-null ``defensive_contribution`` row for every non-quarantined player, keyed
    by ``fpl_code``, as ``(season_id, gameweek_deadline, defensive_contribution, position_short_name)``
    tuples -- a dedicated loader (mirrors ``_load_early_gameweek_fixture_rows``'s own "one small,
    purpose-built query per new signal" precedent) rather than further overloading
    ``_load_all_stats_by_fpl_code``'s shared ``PriorGameweekStat`` shape with a per-row position
    field only this one signal needs.

    Restricted to DEF/MID rows only (see ``DC_CBIT_THRESHOLD_BY_POSITION`` -- FWD/GKP don't earn
    this bonus in any way this module models). Real, confirmed via direct query against the real
    dev database (2026-08-18): ``defensive_contribution`` is NULL for every row before the 2025-26
    season (the rule didn't exist), fully populated for every 2025-26 row -- so this dict is
    populated ONLY from 2025-26 rows in practice today, exactly as documented on
    ``DC_RELIABILITY_COEFFICIENT_SWEEP_CANDIDATES``.
    """
    rows = session.execute(
        select(
            Player.fpl_code,
            Player.season_id,
            Gameweek.deadline_time,
            PlayerGameweekStats.defensive_contribution,
            PlayerGameweekStats.position_short_name_at_time,
        )
        .join(PlayerGameweekStats, PlayerGameweekStats.player_id == Player.id)
        .join(Gameweek, Gameweek.id == PlayerGameweekStats.gameweek_id)
        .where(
            Player.fpl_code.is_not(None),
            Player.is_quarantined.is_(False),
            PlayerGameweekStats.defensive_contribution.is_not(None),
            PlayerGameweekStats.position_short_name_at_time.in_(list(DC_CBIT_THRESHOLD_BY_POSITION)),
        )
    ).all()

    result: dict[int, list[tuple[uuid.UUID, datetime, int, str]]] = defaultdict(list)
    for row in rows:
        result[row.fpl_code].append(
            (row.season_id, row.deadline_time, row.defensive_contribution, row.position_short_name_at_time)
        )
    return result


def _dc_hit_rate(
    rows: Sequence[tuple[uuid.UUID, datetime, int, str]],
    position_short_name: str,
    cutoff: datetime,
) -> tuple[float, int] | None:
    """Real threshold-hit rate (fraction of games meeting ``DC_CBIT_THRESHOLD_BY_POSITION`` for
    ``position_short_name``) across every row in ``rows`` strictly before ``cutoff`` -- ``None`` if
    zero such rows exist (nothing to compute a rate from, not a false ``0.0``).
    """
    threshold = DC_CBIT_THRESHOLD_BY_POSITION[position_short_name]
    prior_rows = [row for row in rows if row[1] < cutoff]
    if not prior_rows:
        return None
    hits = sum(1 for row in prior_rows if row[2] >= threshold)
    return hits / len(prior_rows), len(prior_rows)


def defensive_contribution_hit_rate_prior_seasons(
    session: Session, fpl_code: int, position_short_name: str, as_of: datetime
) -> tuple[float, int] | None:
    """The real, live, PURELY INFORMATIONAL defensive-contribution reliability signal for one
    player -- their real historical rate of hitting ``DC_CBIT_THRESHOLD_BY_POSITION`` across every
    ``defensive_contribution``-carrying gameweek strictly before ``as_of``, or ``None`` if they have
    none (the common case today -- see this module's own docstring on real data availability).

    NOT used anywhere in ``app.optimization.projection``'s actual ``projected_points`` calculation
    -- ``sweep_dc_reliability_coefficient_candidates`` currently has zero real observations to
    validate ANY coefficient against, so wiring this into the live formula would be an unvalidated,
    intuition-driven change, not evidence-based. Exposed instead as a real, honest, informational
    field on ``GET /players/{id}/projection`` (see ``apps.api.routers.players``) so a human reader
    can see the same real signal directly rather than needing to hand-query
    ``player_gameweek_stats.defensive_contribution`` themselves, without the API pretending it
    already shapes the number returned.

    Only meaningful for DEF/MID (``position_short_name`` outside ``DC_CBIT_THRESHOLD_BY_POSITION``
    returns ``None`` immediately -- this bonus isn't concentrated in FWD/GKP play in any way this
    module models).
    """
    if position_short_name not in DC_CBIT_THRESHOLD_BY_POSITION:
        return None
    rows = _load_dc_rows_by_fpl_code(session).get(fpl_code, [])
    return _dc_hit_rate(rows, position_short_name, as_of)


def build_dc_reliability_observations(
    session: Session, early_season_gameweek_cutoff: int = engineering.DEFAULT_EARLY_SEASON_GAMEWEEK_CUTOFF
) -> tuple[list[DcReliabilityObservation], list[str]]:
    """Every real, leakage-safe (player, target season) observation for the DC-reliability
    adjustment, DEF/MID only -- reuses ``build_player_season_observations``'s exact same target-season/
    ``prior_stats``/``career_avg`` machinery (the "actual" being predicted, and the isolated
    ``career_avg_early_season_points_prior_seasons`` baseline being adjusted, are IDENTICAL to that
    function's own -- this only adds the DC-hit-rate filter/lookup on top), further requiring:

    - This player's TARGET-season position (via ``_load_earliest_early_gameweek_price_and_position``,
      the same real, non-leaky "earliest early-season row this season" resolution
      ``build_true_rookie_observations`` already uses) is DEF or MID.
    - At least one real ``defensive_contribution``-carrying gameweek exists in this player's
      STRICTLY PRIOR seasons (``_dc_hit_rate``, cutoff at season S's own earliest gameweek deadline
      -- the same leakage boundary every other builder in this module uses).

    HONEST, REAL RESULT (confirmed against the real dev database, not assumed): this currently
    returns ZERO observations, always, for every real season. ``defensive_contribution`` is only
    non-null starting the 2025-26 season (the CBIT rule's first real season) -- so the ONLY season
    that could ever satisfy "has real DC data in strictly-prior seasons" is one played strictly
    after 2025-26, i.e. 2026-27 -- which has no real ``player_gameweek_stats`` rows yet (this app's
    whole reason for having a cold-start formula at all). This function is real, correct, and fully
    exercised by synthetic-data unit tests -- it simply has nothing real to evaluate yet. The FIRST
    real evaluable observations will appear automatically, with no code changes needed, once 2026-27
    gameweeks start being ingested with real ``defensive_contribution`` values of their own (letting
    a LATER target season use 2026-27 as a strictly-prior source) -- re-run
    ``COLD_START_PROJECTION_VALIDATION`` at that point and treat its real output as first-time
    evidence, not a re-confirmation of anything checked here.
    """
    usable = usable_season_labels_with_gameweek_stats(session)
    if len(usable) < 2:
        return [], usable

    season_rows = session.execute(select(Season.id, Season.label).where(Season.label.in_(usable))).all()
    season_id_by_label = {label: season_id for season_id, label in season_rows}
    eval_labels = usable[1:]
    eval_season_ids = [season_id_by_label[label] for label in eval_labels]

    gw1_deadline_by_season_id = _earliest_gameweek_deadline_by_season(
        session, list(season_id_by_label.values())
    )
    stats_by_fpl_code = _load_all_stats_by_fpl_code(session)
    price_position_by_key = _load_earliest_early_gameweek_price_and_position(
        session, eval_season_ids, early_season_gameweek_cutoff
    )
    dc_rows_by_fpl_code = _load_dc_rows_by_fpl_code(session)

    observations: list[DcReliabilityObservation] = []
    for season_label in eval_labels:
        season_id = season_id_by_label[season_label]
        season_id_str = str(season_id)
        gw1_deadline = gw1_deadline_by_season_id[season_id]

        for fpl_code, all_stats in stats_by_fpl_code.items():
            current_season_stats = [stat for stat in all_stats if stat.season_id == season_id_str]
            if not current_season_stats:
                continue
            actual = engineering.career_avg_early_season_points_prior_seasons(
                current_season_stats, early_season_gameweek_cutoff, half_life_seasons=1.0
            )
            if actual is None:
                continue

            prior_stats = engineering.filter_to_strictly_prior(all_stats, gw1_deadline)
            if not prior_stats:
                continue
            career_avg_baseline = engineering.career_avg_early_season_points_prior_seasons(
                prior_stats, early_season_gameweek_cutoff, engineering.DEFAULT_EARLY_SEASON_HALF_LIFE_SEASONS
            )
            if career_avg_baseline is None:
                continue

            price_position = price_position_by_key.get((fpl_code, season_id))
            if price_position is None:
                continue
            _price, position_short_name = price_position
            if position_short_name not in DC_CBIT_THRESHOLD_BY_POSITION:
                continue

            hit_rate_result = _dc_hit_rate(
                dc_rows_by_fpl_code.get(fpl_code, []), position_short_name, gw1_deadline
            )
            if hit_rate_result is None:
                continue
            prior_dc_hit_rate, prior_dc_games = hit_rate_result

            observations.append(
                DcReliabilityObservation(
                    fpl_code=fpl_code,
                    season_label=season_label,
                    position_short_name=position_short_name,
                    actual_early_season_avg=float(actual),
                    career_avg_baseline=float(career_avg_baseline),
                    prior_dc_hit_rate=prior_dc_hit_rate,
                    prior_dc_games=prior_dc_games,
                )
            )
    return observations, usable


def sweep_dc_reliability_coefficient_candidates(
    observations: Sequence[DcReliabilityObservation],
    candidates: Sequence[float] = DC_RELIABILITY_COEFFICIENT_SWEEP_CANDIDATES,
) -> list[DcReliabilityCoefficientSweepResult]:
    """Real MAE/Spearman for a "trust this player's aggregate historical average more when it's
    demonstrably driven by a repeatable defensive-work-rate skill" adjustment, per candidate
    coefficient: ``career_avg_baseline * (1 + (prior_dc_hit_rate - 0.5) * coefficient)`` -- centered
    on 0.5 (a coin-flip hit rate is neutral, mirroring ``_cold_start_projection``'s own
    fixture-difficulty formula's "neutral at difficulty 3" structure), so a player who NEVER hits
    the real CBIT/CBIRT threshold is adjusted down, one who ALWAYS does is adjusted up, and
    ``coefficient=0.0`` is an explicit no-adjustment control matching today's actual production
    behavior (this signal isn't wired into ``_cold_start_projection`` at all right now).

    ``career_avg_baseline`` isolates this from ``ep_next`` entirely (same isolation
    ``sweep_difficulty_coefficient_candidates`` uses for the identical reason) -- this sweep's real
    MAE numbers carry none of ``sweep_blend_ratio_candidates``'s own ``ep_next``-proxy caveat.

    HONEST RESULT, always, until real 2026-27 data exists (see ``build_dc_reliability_observations``'s
    own docstring for the full reason): ``observations`` is empty, so every candidate here returns
    ``mae=None``/``spearman=None``/``n_observations=0`` -- this sweep genuinely cannot currently
    determine even the SIGN of a real effect, let alone a magnitude, and must not be used to justify
    changing production behavior until it has real pairs to score.
    """
    results = []
    for coefficient in candidates:
        y_true = [obs.actual_early_season_avg for obs in observations]
        y_pred = [
            obs.career_avg_baseline * (1 + (obs.prior_dc_hit_rate - 0.5) * coefficient)
            for obs in observations
        ]
        results.append(
            DcReliabilityCoefficientSweepResult(
                coefficient=coefficient,
                mae=metric_fns.mean_absolute_error(y_true, y_pred) if observations else None,
                spearman=metric_fns.spearman_rank_correlation(y_true, y_pred) if observations else None,
                n_observations=len(observations),
            )
        )
    return results
