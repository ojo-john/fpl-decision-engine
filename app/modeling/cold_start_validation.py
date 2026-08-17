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
    "PlayerSeasonObservation",
    "EarlyGameweekActual",
    "HalfLifeSweepResult",
    "DifficultyCoefficientSweepResult",
    "BlendRatioSweepResult",
    "usable_season_labels_with_gameweek_stats",
    "build_player_season_observations",
    "sweep_half_life_candidates",
    "sweep_difficulty_coefficient_candidates",
    "sweep_blend_ratio_candidates",
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
