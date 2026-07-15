"""Pure(ish) chip scenario ranking (Milestone 6 Layer 2: Chip Scenario Ranking).

Composes Milestone 6 Layer 1's primitives (``app.chips.windows``/
``app.chips.inventory``/``app.optimization.fixture_calendar``) with several
already-existing Milestone 2-5 building blocks --
``app.optimization.projection.project_player_with_champion``,
``app.transfers.horizon_projection.compute_horizon_points``,
``app.optimization.joint_squad_solver.solve_joint_squad_and_xi``, and
``app.h2h.simulate``'s Monte Carlo machinery -- into one gain estimate per
candidate gameweek, per chip, then a single recommendation.

This is "scenario ranking, not perfect optimization" (the master spec's own
phrase for this milestone): every evaluator below produces a coarse,
honestly-scoped estimate of a chip's value for a HANDFUL of candidate
gameweeks, not an exhaustive, fully-optimal search over every possible future
chip-timing decision. No job/persistence/REST wiring happens here (Milestone
6 Layer 3's job) -- this module only turns already-resolved inputs into a
``ChipWindowCandidateResult``/``RecommendationResult``, mirroring
``app.h2h.strategy``'s own "generate variants, score each, recommend one,
with a documented tie-break" precedent as closely as any module in this
codebase.

Two very different evaluation costs, on purpose:

- **Bench Boost / Triple Captain are cheap** (a projection lookup + a
  Monte Carlo loop, no MIP solve) -- so they are evaluated EXHAUSTIVELY across
  every unfinished gameweek in the chip's currently-open window (see
  ``resolve_gameweeks_within_window``), never capped.
- **Wildcard / Free Hit are expensive** (each candidate gameweek requires a
  real two-phase lexicographic MIP solve over the ENTIRE live candidate pool,
  ``app.optimization.joint_squad_solver.solve_joint_squad_and_xi`` -- see that
  module's own docstring for why it can't be approximated cheaply) -- so only
  a small, capped, cheaply-pre-filtered candidate set is ever solved for (see
  ``_WILDCARD_MAX_CANDIDATE_GAMEWEEKS`` and ``_select_bounded_candidate_gameweeks``
  below). Running a full MIP solve for every one of up to ~19 unfinished
  gameweeks in a season-long window would be needlessly expensive AND
  pointless precision: Wildcard/Free Hit timing is a coarse, big-picture
  decision (is there a Double Gameweek or a soft run of fixtures coming up?),
  not one that benefits from checking every single week to the same MIP-exact
  precision.

This asymmetry is the literal embodiment of "scenario ranking, not perfect
optimization" -- documented here explicitly rather than left as an implicit
inconsistency between the four evaluators.

Fixture-count awareness (Bench Boost only): ``project_player_with_champion``/
``compute_horizon_points`` are NOT fixture-count-aware (confirmed by reading
``app.optimization.projection`` -- a projection is always a single-fixture
estimate, with no notion of a Blank/Double Gameweek). Bench Boost's own gain
calculation applies a coarse ``drawn_score * TeamFixtureCount.fixture_count``
multiplier to each BENCH player's contribution ONLY (never to the shared
projection formula itself, and never to the starting XI's own baseline --
see ``evaluate_bench_boost_candidate``'s own docstring for exactly why this
is scoped to the bench-only delta) -- because Bench Boost's real strategic
value is maximized precisely when bench players have a Double Gameweek (their
points, uncounted without the chip, would count twice). Triple Captain
deliberately does NOT get this same treatment in this layer: the captain's
own Double Gameweek would matter too in reality, but modeling it correctly
touches the SAME player on both sides of the with/without-chip comparison
(the captain scores from 2 fixtures regardless of which chip -- if any -- is
played), so the naive "multiply the drawn score" trick used for Bench Boost
doesn't cleanly generalize; left as a known, documented Layer 2 limitation
rather than a half-correct heuristic.

Paired Monte Carlo comparison (Bench Boost / Triple Captain): both draw their
"with-chip" and "without-chip" totals for one iteration from the exact SAME
``app.h2h.simulate.sample_player_draws`` call -- never two independently
resampled distributions subtracted (which would inflate variance and could
even yield a nonsensical negative-mean "gain" from pure sampling noise).
Mirrors ``app.h2h.simulate.sample_rival_scores``'s own documented "same
sampled distribution for fair comparison" principle.

Wildcard / Free Hit are NOT purely computational (unlike the other two chips
here) and DO take a real ``Session``: they require solving a real MIP over
the live candidate pool to answer "what's the best 15-man squad I could
afford right now" -- there is no way to meaningfully mock this as "just take
these already-computed numbers" the way Bench Boost/Triple Captain's
projection+residual inputs can be pre-resolved and handed in as plain
dataclasses. Budget = ``bank + sum(current squad's selling prices)`` (mirrors
``app.jobs.handlers.transfer_optimization``'s own budget concept -- a
from-scratch rebuild is "sell everything, buy a new squad", generalizing that
job's "sell one, buy one" scenario search to "sell all 15, buy all 15" at
once). The current squad's own comparison baseline is computed from
caller-supplied starting-XI/captain identity (NOT re-derived via
``app.jobs.handlers.h2h_simulation``'s own autosub resolution --
``_resolve_effective_starting_xi`` is private, and tightly coupled to that
module's own raw-picks-JSON/``UserPlayerInput`` construction; reusing it here
would need its own parallel resolution path anyway, so this is documented as
a known Layer 2 simplification, not a silent gap: an autosub-affected current
squad's baseline could be slightly under/over-stated until a future layer
wires in the same autosub logic for this caller too).
"""

import statistics
import uuid
from dataclasses import dataclass, replace
from random import Random

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.h2h.simulate import UserPlayerInput, sample_player_draws
from app.optimization.candidates import OptimizationCandidate
from app.optimization.champion_loader import LoadedChampion
from app.optimization.constraints import OptimizationConstraints
from app.optimization.fixture_calendar import TeamFixtureCount, resolve_team_fixture_counts_for_gameweek
from app.optimization.joint_squad_solver import JointSquadSolution, solve_joint_squad_and_xi
from app.optimization.projection import project_player_with_champion
from app.transfers.horizon_projection import compute_horizon_points
from packages.database.models.fixture import Fixture
from packages.database.models.gameweek import Gameweek
from packages.database.models.player import Player
from packages.database.models.position import Position
from packages.domain.chips import ChipVerdict
from packages.domain.optimization import SolverStatus

# --- Shared result/ranking shapes (every chip evaluator below produces this) ------------------

_FLOOR_PERCENTILE = 5.0
_CEILING_PERCENTILE = 95.0


@dataclass(frozen=True)
class ChipWindowCandidateResult:
    """One candidate gameweek's evaluated chip-usage gain, ready for ``rank_candidates_for_chip``.

    ``gain_floor``/``gain_ceiling`` equal ``gain_mean`` exactly for the
    MIP-based evaluators (Wildcard/Free Hit): those produce one deterministic
    point estimate per candidate (a single MIP solve, no Monte Carlo
    distribution -- see module docstring for why re-sampling a MIP solve
    would be needlessly expensive without adding real information). Bench
    Boost/Triple Captain's three fields genuinely differ -- true Monte Carlo
    mean/floor/ceiling of the paired gain distribution.
    """

    fpl_event_id: int
    gameweek_id: uuid.UUID
    gain_mean: float
    gain_floor: float
    gain_ceiling: float


@dataclass(frozen=True)
class RecommendationResult:
    """``rank_candidates_for_chip``'s final recommendation for one chip."""

    verdict: ChipVerdict
    recommended_fpl_event_id: int | None
    recommended_gain_mean: float | None
    rationale: str


# A later candidate gameweek's best gain must beat using the chip NOW by more
# than this many points before it's worth recommending HOLD -- FPL single-
# gameweek projections are not reliably accurate to better than a couple of
# points (see app.optimization.projection's own availability-scaling/fallback
# judgment calls), so a smaller gap is noise, not real evidence that waiting
# is better. Mirrors app.h2h.strategy.VARIANCE_ADJUSTMENT_K's own "one
# hand-picked constant, documented, not fit against data" precedent. Applied
# uniformly across all 4 chips even though their gain magnitudes differ
# (Wildcard/Free Hit's horizon-summed gains typically dwarf Bench Boost/
# Triple Captain's single-gameweek gains) -- a known, documented Layer 2
# simplification; a per-chip-type threshold is a plausible future refinement,
# not implemented here.
_CHIP_USE_NOW_MATERIALITY_THRESHOLD = 2.0


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Linear-interpolation percentile -- mirrors ``app.h2h.simulate._percentile``'s own algorithm.

    Reimplemented (not imported) since that function is private to
    ``app.h2h.simulate`` and this module's inputs are floats, not the ints
    ``app.h2h.simulate`` deals in (whole-number FPL scores) -- a parallel,
    not shared, implementation.
    """
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (pct / 100.0) * (len(sorted_values) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = rank - lower
    return sorted_values[lower] + fraction * (sorted_values[upper] - sorted_values[lower])


def rank_candidates_for_chip(candidates: list[ChipWindowCandidateResult]) -> RecommendationResult:
    """Pick USE_NOW/HOLD (or NO_VIABLE_WINDOW) for one chip from its evaluated candidate gameweeks.

    ``candidates`` MUST be ordered with the very next unfinished/evaluated
    gameweek FIRST (``candidates[0]``) -- every ``evaluate_*`` function in
    this module already produces its results in that order (ascending by
    deadline for Bench Boost/Triple Captain's exhaustive scan; "immediate
    first, then the cost-bounded rest" for Wildcard/Free Hit).

    Logic, in order:

    1. No candidates at all (the window is already closed, or genuinely
       nothing could be evaluated) -> ``NO_VIABLE_WINDOW``.
    2. The single best-``gain_mean`` candidate (ties broken toward the
       EARLIER gameweek) is non-positive -> ``HOLD``: no evaluated gameweek
       is worth using the chip for at all right now. Deliberately NOT
       ``NO_VIABLE_WINDOW`` -- the chip genuinely remains usable and a later
       gameweek could still turn positive; this is an honest "not worth it
       yet" verdict, not "there was nothing to evaluate".
    3. The best candidate IS the immediate one, or beats it by no more than
       ``_CHIP_USE_NOW_MATERIALITY_THRESHOLD`` points -> ``USE_NOW`` (waiting
       for an immaterially-better later week isn't worth forfeiting a chip
       use now, given real projection uncertainty).
    4. Otherwise, a strictly later candidate clearly beats using the chip
       now -> ``HOLD``, naming that later gameweek and the exact gain delta
       (the literal "expected value of saving the chip" the master spec
       requires).
    """
    if not candidates:
        return RecommendationResult(
            verdict=ChipVerdict.NO_VIABLE_WINDOW,
            recommended_fpl_event_id=None,
            recommended_gain_mean=None,
            rationale="No candidate gameweek could be evaluated within this chip's currently-open window.",
        )

    immediate = candidates[0]
    best = min(candidates, key=lambda c: (-c.gain_mean, c.fpl_event_id))

    if best.gain_mean <= 0:
        return RecommendationResult(
            verdict=ChipVerdict.HOLD,
            recommended_fpl_event_id=None,
            recommended_gain_mean=None,
            rationale=(
                f"No evaluated gameweek shows a positive expected gain (best: GW{best.fpl_event_id} at "
                f"{best.gain_mean:+.2f} points); holding the chip rather than using it for zero or "
                "negative benefit."
            ),
        )

    if (
        best.fpl_event_id == immediate.fpl_event_id
        or (best.gain_mean - immediate.gain_mean) <= _CHIP_USE_NOW_MATERIALITY_THRESHOLD
    ):
        return RecommendationResult(
            verdict=ChipVerdict.USE_NOW,
            recommended_fpl_event_id=immediate.fpl_event_id,
            recommended_gain_mean=immediate.gain_mean,
            rationale=(
                f"Use now: GW{immediate.fpl_event_id}'s expected gain ({immediate.gain_mean:+.2f} points) "
                f"is already within {_CHIP_USE_NOW_MATERIALITY_THRESHOLD:.1f} points of the best evaluated "
                "alternative, so waiting is not worth forfeiting a use now."
            ),
        )

    delta = best.gain_mean - immediate.gain_mean
    return RecommendationResult(
        verdict=ChipVerdict.HOLD,
        recommended_fpl_event_id=best.fpl_event_id,
        recommended_gain_mean=best.gain_mean,
        rationale=(
            f"Hold: GW{best.fpl_event_id}'s expected gain ({best.gain_mean:+.2f} points) beats using the "
            f"chip now in GW{immediate.fpl_event_id} ({immediate.gain_mean:+.2f} points) by {delta:+.2f} "
            f"points -- more than the {_CHIP_USE_NOW_MATERIALITY_THRESHOLD:.1f}-point materiality threshold."
        ),
    )


# --- Bench Boost ----------------------------------------------------------------------------

# Fewer draws than app.h2h.simulate.SAMPLE_COUNT (5,000): a chip's gain
# mean/floor/ceiling needs far less precision than a full H2H matchup
# win/draw/loss probability, and this sample count is paid EXHAUSTIVELY
# across every unfinished gameweek in a chip's window (unlike Wildcard/Free
# Hit's capped MIP solves) -- kept small so that repeated cost stays cheap.
_CHIP_SAMPLE_COUNT = 2000


@dataclass(frozen=True)
class BenchPlayerInput:
    """One bench player, for Bench Boost gain evaluation.

    ``team_id`` is needed (unlike ``UserPlayerInput``, which has no concept
    of a team) to look up this specific player's own ``TeamFixtureCount`` for
    the gameweek being evaluated -- a bench player's team can have a
    different fixture count than a teammate starter's team.
    """

    player_id: str
    team_id: uuid.UUID
    projected_points: float
    own_residuals: tuple[float, ...] = ()
    position_residuals: tuple[float, ...] = ()


@dataclass(frozen=True)
class BenchBoostGameweekCandidate:
    """Everything needed to evaluate Bench Boost for ONE candidate gameweek.

    ``starters`` are ``UserPlayerInput``s with each starter's REAL multiplier
    for this gameweek already applied (1 normal, 2 captain) -- read verbatim
    from the resolved XI, exactly like ``app.h2h.simulate.UserPlayerInput.multiplier``'s
    own contract. ``team_fixture_counts`` should be scoped to this ONE
    gameweek (see ``app.optimization.fixture_calendar.resolve_team_fixture_counts_for_gameweek``).
    """

    fpl_event_id: int
    gameweek_id: uuid.UUID
    starters: tuple[UserPlayerInput, ...]
    bench: tuple[BenchPlayerInput, ...]
    team_fixture_counts: dict[uuid.UUID, TeamFixtureCount]


def _bench_fixture_multiplier(
    bench_player: BenchPlayerInput, team_fixture_counts: dict[uuid.UUID, TeamFixtureCount]
) -> float:
    """A bench player's coarse fixture-count multiplier for this gameweek (0 blank / 1 normal / 2+ double).

    Defaults to 1 (a normal single fixture) when this player's team is absent
    from ``team_fixture_counts`` entirely -- a safe, non-punitive default,
    never a crash, for a caller that only resolved fixture counts for a
    subset of teams.
    """
    count = team_fixture_counts.get(bench_player.team_id)
    return float(count.fixture_count) if count is not None else 1.0


def _paired_bench_boost_gain_samples(
    rng: Random,
    starters: tuple[UserPlayerInput, ...],
    bench: tuple[BenchPlayerInput, ...],
    bench_multipliers: tuple[float, ...],
    sample_count: int,
) -> tuple[float, ...]:
    """One paired (with-chip total - without-chip total) sample per Monte Carlo iteration.

    Both totals are derived from the SAME ``sample_player_draws`` call within
    one iteration (never two independently-resampled totals subtracted) --
    see module docstring's "Paired Monte Carlo comparison" section. Written
    out explicitly (rather than directly computing the bench sum, to which
    this is algebraically equivalent) so the pairing is structurally obvious
    and independently testable.
    """
    bench_as_players = tuple(
        UserPlayerInput(
            player_id=b.player_id,
            projected_points=b.projected_points,
            multiplier=1,
            own_residuals=b.own_residuals,
            position_residuals=b.position_residuals,
        )
        for b in bench
    )
    all_players = starters + bench_as_players
    n_starters = len(starters)

    gains: list[float] = []
    for _ in range(sample_count):
        draws = sample_player_draws(rng, all_players)
        starter_draws = draws[:n_starters]
        bench_draws = draws[n_starters:]
        without_chip_total = sum(d * p.multiplier for d, p in zip(starter_draws, starters, strict=True))
        bench_component = sum(d * m for d, m in zip(bench_draws, bench_multipliers, strict=True))
        with_chip_total = without_chip_total + bench_component
        gains.append(float(with_chip_total - without_chip_total))
    return tuple(gains)


def evaluate_bench_boost_candidate(
    candidate: BenchBoostGameweekCandidate, rng: Random, sample_count: int = _CHIP_SAMPLE_COUNT
) -> ChipWindowCandidateResult:
    """Bench Boost's Monte Carlo gain (mean/floor/ceiling) for ONE candidate gameweek.

    Gain = sum of the 4 bench players' drawn scores, each scaled by that
    player's OWN team's fixture-count multiplier for this gameweek (see
    module docstring's "Fixture-count awareness" section) -- the starting
    XI's own baseline is identical with or without the chip and therefore
    never needs its own fixture-count correction for THIS gain calculation
    (it cancels out algebraically; see ``_paired_bench_boost_gain_samples``).
    """
    bench_multipliers = tuple(
        _bench_fixture_multiplier(b, candidate.team_fixture_counts) for b in candidate.bench
    )
    gains = _paired_bench_boost_gain_samples(
        rng, candidate.starters, candidate.bench, bench_multipliers, sample_count
    )
    sorted_gains = sorted(gains)
    return ChipWindowCandidateResult(
        fpl_event_id=candidate.fpl_event_id,
        gameweek_id=candidate.gameweek_id,
        gain_mean=statistics.fmean(gains),
        gain_floor=_percentile(sorted_gains, _FLOOR_PERCENTILE),
        gain_ceiling=_percentile(sorted_gains, _CEILING_PERCENTILE),
    )


def evaluate_bench_boost_window(
    candidates: list[BenchBoostGameweekCandidate], rng: Random, sample_count: int = _CHIP_SAMPLE_COUNT
) -> list[ChipWindowCandidateResult]:
    """Evaluate EVERY given candidate gameweek -- no cap (see module docstring's cost asymmetry).

    ``candidates`` is expected to already be every unfinished gameweek in the
    chip's currently-open window (see
    ``app.optimization.gameweek_resolution.resolve_gameweeks_within_window``),
    in ascending order -- this function preserves that order in its output.
    """
    return [evaluate_bench_boost_candidate(c, rng, sample_count) for c in candidates]


# --- Triple Captain --------------------------------------------------------------------------

# Top-3 highest-projected starters is enough breadth to catch a genuine
# risk/reward tradeoff (e.g. a nailed-on player with a slightly lower
# ceiling vs. a rotation risk with a higher one) without evaluating all 11
# starters -- mirrors app.h2h.strategy's own CAPTAIN_COVER top-2 pool-size
# precedent, widened by one since Triple Captain's downside (a blank from
# the TRIPLED player) is more punishing than a normal captain's, worth
# checking one extra alternative.
_TRIPLE_CAPTAIN_CANDIDATE_POOL_SIZE = 3


@dataclass(frozen=True)
class TripleCaptainGameweekCandidate:
    """Everything needed to evaluate Triple Captain for ONE candidate gameweek.

    ``captain_pool`` should already be trimmed to the top
    ``_TRIPLE_CAPTAIN_CANDIDATE_POOL_SIZE`` starters by ``projected_points``
    (each with ``multiplier=1`` -- this evaluator applies its own captain
    multipliers internally and ignores whatever multiplier is set here).
    """

    fpl_event_id: int
    gameweek_id: uuid.UUID
    captain_pool: tuple[UserPlayerInput, ...]


def top_captain_candidates(
    starters: tuple[UserPlayerInput, ...], pool_size: int = _TRIPLE_CAPTAIN_CANDIDATE_POOL_SIZE
) -> tuple[UserPlayerInput, ...]:
    """The top ``pool_size`` starters by ``projected_points`` (descending), ties broken by ``player_id``.

    A small, reusable helper so a future Layer 3 caller building
    ``TripleCaptainGameweekCandidate.captain_pool`` doesn't need to
    reimplement this trivial ranking itself.
    """
    ranked = sorted(starters, key=lambda p: (-p.projected_points, p.player_id))
    return tuple(ranked[:pool_size])


def _paired_triple_captain_gain_samples(
    rng: Random, player: UserPlayerInput, captain_multiplier: int, sample_count: int
) -> tuple[float, ...]:
    """One paired (with-chip total - without-chip total) sample per Monte Carlo iteration, for
    Triple-Captaining ONE specific player. Both totals share the exact same per-iteration draw --
    see module docstring's "Paired Monte Carlo comparison" section.
    """
    solo = (player,)
    gains: list[float] = []
    for _ in range(sample_count):
        draw = sample_player_draws(rng, solo)[0]
        without_chip_total = draw * captain_multiplier
        with_chip_total = draw * (captain_multiplier + 1)
        gains.append(float(with_chip_total - without_chip_total))
    return tuple(gains)


def evaluate_triple_captain_candidate(
    candidate: TripleCaptainGameweekCandidate,
    captain_multiplier: int,
    rng: Random,
    sample_count: int = _CHIP_SAMPLE_COUNT,
) -> ChipWindowCandidateResult:
    """Best achievable Triple Captain incremental gain for ONE candidate gameweek.

    Evaluates every ``candidate.captain_pool`` member's OWN incremental gain
    (their own drawn score, once more -- the +1x extra multiplier unit over
    the normal ``captain_multiplier``) and returns whichever yields the
    highest MEAN gain -- genuinely Monte Carlo, not just whoever has the
    single highest raw projection, since a lower-projected-but-safer captain
    candidate could plausibly have a better risk-adjusted incremental gain
    once residual variance is considered.
    """
    if not candidate.captain_pool:
        return ChipWindowCandidateResult(candidate.fpl_event_id, candidate.gameweek_id, 0.0, 0.0, 0.0)

    best: ChipWindowCandidateResult | None = None
    for player in candidate.captain_pool:
        gains = _paired_triple_captain_gain_samples(rng, player, captain_multiplier, sample_count)
        sorted_gains = sorted(gains)
        result = ChipWindowCandidateResult(
            fpl_event_id=candidate.fpl_event_id,
            gameweek_id=candidate.gameweek_id,
            gain_mean=statistics.fmean(gains),
            gain_floor=_percentile(sorted_gains, _FLOOR_PERCENTILE),
            gain_ceiling=_percentile(sorted_gains, _CEILING_PERCENTILE),
        )
        if best is None or result.gain_mean > best.gain_mean:
            best = result
    assert best is not None  # candidate.captain_pool is non-empty (checked above)
    return best


def evaluate_triple_captain_window(
    candidates: list[TripleCaptainGameweekCandidate],
    captain_multiplier: int,
    rng: Random,
    sample_count: int = _CHIP_SAMPLE_COUNT,
) -> list[ChipWindowCandidateResult]:
    """Evaluate EVERY given candidate gameweek -- no cap (see module docstring's cost asymmetry).

    Same ordering contract as ``evaluate_bench_boost_window``.
    """
    return [evaluate_triple_captain_candidate(c, captain_multiplier, rng, sample_count) for c in candidates]


# --- Wildcard / Free Hit ----------------------------------------------------------------------

# Solving a fresh 15-player MIP for every unfinished gameweek in a
# season-long window (up to ~19) is both prohibitively expensive and
# pointless precision for a genuinely coarse timing decision (see module
# docstring) -- so only the immediate gameweek plus this many additional,
# cheaply-pre-filtered candidates are ever solved for.
_WILDCARD_MAX_CANDIDATE_GAMEWEEKS = 4

# FPL's own fixture difficulty scale is 1 (easiest) - 5 (hardest); this
# midpoint is used only when a fixture's difficulty is entirely unrecorded,
# so an unrated gameweek never biases the cheap heuristic ranking in either
# direction.
_NEUTRAL_FIXTURE_DIFFICULTY = 3.0


def _fpl_event_id_for(session: Session, gameweek_id: uuid.UUID) -> int:
    return session.execute(select(Gameweek.fpl_event_id).where(Gameweek.id == gameweek_id)).scalar_one()


def _cheap_gameweek_heuristic_score(
    session: Session, season_id: uuid.UUID, gameweek_id: uuid.UUID
) -> tuple[int, float]:
    """Ascending-sort key: prefer MORE teams in a Double Gameweek, then LOWER average fixture difficulty.

    A computationally-cheap stand-in for "how much extra value could a
    freshly rebuilt squad extract from this particular gameweek", built
    entirely from already-ingested data (Layer 1's fixture-count resolution
    plus ``Fixture.home_difficulty``/``away_difficulty``) -- no MIP solve
    needed just to RANK candidates, only to actually score the ones that
    make the cut.
    """
    counts = resolve_team_fixture_counts_for_gameweek(session, season_id, gameweek_id)
    double_gameweek_team_count = sum(1 for c in counts.values() if c.fixture_count >= 2)

    fixtures = (
        session.execute(
            select(Fixture).where(
                Fixture.season_id == season_id,
                Fixture.gameweek_id == gameweek_id,
                Fixture.is_quarantined.is_(False),
            )
        )
        .scalars()
        .all()
    )
    difficulties = [d for f in fixtures for d in (f.home_difficulty, f.away_difficulty) if d is not None]
    avg_difficulty = statistics.fmean(difficulties) if difficulties else _NEUTRAL_FIXTURE_DIFFICULTY

    return (-double_gameweek_team_count, avg_difficulty)


def _select_bounded_candidate_gameweeks(
    session: Session, season_id: uuid.UUID, window_gameweek_ids: list[uuid.UUID]
) -> list[uuid.UUID]:
    """The immediate gameweek plus up to ``_WILDCARD_MAX_CANDIDATE_GAMEWEEKS - 1`` more, heuristically ranked.

    ``window_gameweek_ids`` is assumed already ascending by deadline (see
    ``resolve_gameweeks_within_window``). The very first (soonest) gameweek
    is ALWAYS included regardless of its own heuristic score -- "use it
    literally right now" must always be a real, evaluated option, never
    filtered out by the cheap ranking. The remaining slots are filled by the
    best-scoring candidates among the rest of the window.
    """
    if not window_gameweek_ids:
        return []
    immediate, rest = window_gameweek_ids[0], window_gameweek_ids[1:]
    remaining_slots = _WILDCARD_MAX_CANDIDATE_GAMEWEEKS - 1
    if not rest or remaining_slots <= 0:
        return [immediate]
    ranked_rest = sorted(rest, key=lambda gw_id: _cheap_gameweek_heuristic_score(session, season_id, gw_id))
    return [immediate, *ranked_rest[:remaining_slots]]


def _rebuild_budget(bank: int, selling_prices: dict[uuid.UUID, int]) -> int:
    """Budget for a from-scratch squad rebuild: bank + total selling value of the current squad.

    Mirrors ``app.jobs.handlers.transfer_optimization``'s own budget concept
    (bank + selling prices funds every transfer) -- generalized here to "the
    whole squad is being sold and rebought at once", not that job's
    incremental "sell one, buy one" scenario search.
    """
    return bank + sum(selling_prices.values())


def _build_full_candidate_pool(
    session: Session, season_id: uuid.UUID, target_gameweek_id: uuid.UUID, champion: LoadedChampion | None
) -> list[OptimizationCandidate]:
    """The full eligible candidate pool for a from-scratch squad rebuild.

    Deliberately mirrors ``app.jobs.handlers.squad_optimization._build_candidates``
    exactly (non-quarantined players with a resolved position/team, champion-
    aware single-gameweek projection, eligible-only) -- duplicated here
    rather than imported, since that function is private to its own
    job-handler module. A future layer may extract one shared helper for
    both call sites; this layer only needs the computation to be correct,
    not to eliminate this one small, documented duplication.
    """
    players = (
        session.execute(select(Player).where(Player.season_id == season_id, Player.is_quarantined.is_(False)))
        .scalars()
        .all()
    )
    positions_by_id = {
        p.id: p for p in session.execute(select(Position).where(Position.season_id == season_id)).scalars()
    }

    candidates: list[OptimizationCandidate] = []
    for player in players:
        if player.team_id is None or player.position_id is None:
            continue
        position = positions_by_id.get(player.position_id)
        if position is None:
            continue
        projection = project_player_with_champion(session, player, target_gameweek_id, champion)
        if not projection.is_eligible:
            continue
        candidates.append(
            OptimizationCandidate(
                player_id=player.id,
                projected_points=projection.projected_points,
                projection_source=projection.source,
                position_short_name=position.short_name,
                team_id=player.team_id,
                now_cost=player.now_cost,
            )
        )
    return candidates


def _solve_rebuilt_squad(
    session: Session,
    season_id: uuid.UUID,
    target_gameweek_id: uuid.UUID,
    champion: LoadedChampion | None,
    constraints: OptimizationConstraints,
    captain_multiplier: int,
    budget: int,
) -> JointSquadSolution:
    candidates = _build_full_candidate_pool(session, season_id, target_gameweek_id, champion)
    rebuild_constraints = replace(constraints, total_spend_limit=budget)
    return solve_joint_squad_and_xi(candidates, rebuild_constraints, captain_multiplier)


def _sum_horizon_points_for_xi(
    session: Session,
    starting_player_ids: list[uuid.UUID],
    captain_player_id: uuid.UUID,
    horizon_gameweek_ids: list[uuid.UUID],
    captain_multiplier: int,
    champion: LoadedChampion | None,
) -> float:
    """Sum of ``compute_horizon_points`` across every starter, with the captain's own horizon points
    counted an extra ``captain_multiplier - 1`` times -- the horizon-summed generalization of
    ``JointSquadSolution.xi_projected_points``'s own single-gameweek formula.

    A starting-XI player id that no longer resolves to a ``Player`` row
    contributes 0 rather than raising -- degrades gracefully, same spirit as
    every other per-player resolution in this codebase.
    """
    total = 0.0
    for player_id in starting_player_ids:
        player = session.get(Player, player_id)
        if player is None:
            continue
        points = compute_horizon_points(session, player, horizon_gameweek_ids, champion)
        total += points
        if player_id == captain_player_id:
            total += (captain_multiplier - 1) * points
    return total


def _single_gameweek_xi_points(
    session: Session,
    starting_player_ids: list[uuid.UUID],
    captain_player_id: uuid.UUID,
    target_gameweek_id: uuid.UUID,
    captain_multiplier: int,
    champion: LoadedChampion | None,
) -> float:
    """Single-gameweek equivalent of ``_sum_horizon_points_for_xi``, for Free Hit's one-week comparison."""
    total = 0.0
    for player_id in starting_player_ids:
        player = session.get(Player, player_id)
        if player is None:
            continue
        points = project_player_with_champion(session, player, target_gameweek_id, champion).projected_points
        total += points
        if player_id == captain_player_id:
            total += (captain_multiplier - 1) * points
    return total


def evaluate_wildcard_candidate(
    session: Session,
    season_id: uuid.UUID,
    target_gameweek_id: uuid.UUID,
    fpl_event_id: int,
    horizon_gameweek_ids: list[uuid.UUID],
    current_squad_starting_player_ids: list[uuid.UUID],
    current_squad_captain_player_id: uuid.UUID,
    bank: int,
    selling_prices: dict[uuid.UUID, int],
    constraints: OptimizationConstraints,
    captain_multiplier: int,
    champion: LoadedChampion | None,
) -> ChipWindowCandidateResult | None:
    """Wildcard's gain for ONE candidate gameweek: rebuilt squad's horizon points minus the current
    squad's equivalent, both summed across ``horizon_gameweek_ids`` (the "remaining current-window
    horizon" -- see module/``evaluate_wildcard`` docstrings for why this is a suffix of the window,
    not a fixed length).

    Returns ``None`` if the rebuild MIP is INFEASIBLE/ERROR for this
    candidate (e.g. an emptied candidate pool) -- never raises; this
    candidate is simply excluded from ranking, exactly like every other
    caller of ``solve_joint_squad_and_xi`` treats a non-OPTIMAL solve.
    """
    budget = _rebuild_budget(bank, selling_prices)
    solution = _solve_rebuilt_squad(
        session, season_id, target_gameweek_id, champion, constraints, captain_multiplier, budget
    )
    if solution.solver_status != SolverStatus.OPTIMAL:
        return None
    assert solution.captain_player_id is not None

    rebuilt_points = _sum_horizon_points_for_xi(
        session,
        solution.starting_player_ids,
        solution.captain_player_id,
        horizon_gameweek_ids,
        captain_multiplier,
        champion,
    )
    current_points = _sum_horizon_points_for_xi(
        session,
        current_squad_starting_player_ids,
        current_squad_captain_player_id,
        horizon_gameweek_ids,
        captain_multiplier,
        champion,
    )
    gain = rebuilt_points - current_points
    return ChipWindowCandidateResult(fpl_event_id, target_gameweek_id, gain, gain, gain)


def evaluate_wildcard(
    session: Session,
    season_id: uuid.UUID,
    window_gameweek_ids: list[uuid.UUID],
    current_squad_starting_player_ids: list[uuid.UUID],
    current_squad_captain_player_id: uuid.UUID,
    bank: int,
    selling_prices: dict[uuid.UUID, int],
    constraints: OptimizationConstraints,
    captain_multiplier: int,
    champion: LoadedChampion | None,
) -> list[ChipWindowCandidateResult]:
    """Evaluate a small, capped, heuristically pre-filtered set of candidate gameweeks (see module
    docstring's cost-bounding rationale) within the currently-open Wildcard window.

    ``window_gameweek_ids`` should be every unfinished gameweek in the
    Wildcard window (see ``app.optimization.gameweek_resolution.resolve_gameweeks_within_window``
    with that window's own ``stop_event``), ascending by deadline. For each
    selected candidate, the comparison horizon is the "remaining current-
    window horizon" -- every unfinished gameweek from that candidate through
    the window's own ``stop_event`` (a SUFFIX of ``window_gameweek_ids``, not
    a fixed length) -- since Wildcard's real benefit (unlimited transfers)
    persists for as long as the window stays open, naturally shrinking for
    later candidates.
    """
    bounded_gameweek_ids = _select_bounded_candidate_gameweeks(session, season_id, window_gameweek_ids)
    results: list[ChipWindowCandidateResult] = []
    for gameweek_id in bounded_gameweek_ids:
        position = window_gameweek_ids.index(gameweek_id)
        horizon_ids = window_gameweek_ids[position:]
        fpl_event_id = _fpl_event_id_for(session, gameweek_id)
        result = evaluate_wildcard_candidate(
            session,
            season_id,
            gameweek_id,
            fpl_event_id,
            horizon_ids,
            current_squad_starting_player_ids,
            current_squad_captain_player_id,
            bank,
            selling_prices,
            constraints,
            captain_multiplier,
            champion,
        )
        if result is not None:
            results.append(result)
    return results


def evaluate_free_hit_candidate(
    session: Session,
    season_id: uuid.UUID,
    target_gameweek_id: uuid.UUID,
    fpl_event_id: int,
    current_squad_starting_player_ids: list[uuid.UUID],
    current_squad_captain_player_id: uuid.UUID,
    bank: int,
    selling_prices: dict[uuid.UUID, int],
    constraints: OptimizationConstraints,
    captain_multiplier: int,
    champion: LoadedChampion | None,
) -> ChipWindowCandidateResult | None:
    """Free Hit's gain for ONE candidate gameweek: rebuilt squad's SINGLE-gameweek XI points minus
    the current squad's equivalent for that same one gameweek only -- the squad reverts afterward,
    so (unlike Wildcard) there is no horizon summing here at all.

    Returns ``None`` on a non-OPTIMAL rebuild solve -- same convention as
    ``evaluate_wildcard_candidate``.
    """
    budget = _rebuild_budget(bank, selling_prices)
    solution = _solve_rebuilt_squad(
        session, season_id, target_gameweek_id, champion, constraints, captain_multiplier, budget
    )
    if solution.solver_status != SolverStatus.OPTIMAL:
        return None
    assert solution.xi_projected_points is not None

    current_points = _single_gameweek_xi_points(
        session,
        current_squad_starting_player_ids,
        current_squad_captain_player_id,
        target_gameweek_id,
        captain_multiplier,
        champion,
    )
    gain = solution.xi_projected_points - current_points
    return ChipWindowCandidateResult(fpl_event_id, target_gameweek_id, gain, gain, gain)


def evaluate_free_hit(
    session: Session,
    season_id: uuid.UUID,
    window_gameweek_ids: list[uuid.UUID],
    current_squad_starting_player_ids: list[uuid.UUID],
    current_squad_captain_player_id: uuid.UUID,
    bank: int,
    selling_prices: dict[uuid.UUID, int],
    constraints: OptimizationConstraints,
    captain_multiplier: int,
    champion: LoadedChampion | None,
) -> list[ChipWindowCandidateResult]:
    """Same capped, heuristically pre-filtered candidate selection as ``evaluate_wildcard``, but each
    candidate is scored as a single-gameweek Free Hit (see ``evaluate_free_hit_candidate``), never
    horizon-summed.
    """
    bounded_gameweek_ids = _select_bounded_candidate_gameweeks(session, season_id, window_gameweek_ids)
    results: list[ChipWindowCandidateResult] = []
    for gameweek_id in bounded_gameweek_ids:
        fpl_event_id = _fpl_event_id_for(session, gameweek_id)
        result = evaluate_free_hit_candidate(
            session,
            season_id,
            gameweek_id,
            fpl_event_id,
            current_squad_starting_player_ids,
            current_squad_captain_player_id,
            bank,
            selling_prices,
            constraints,
            captain_multiplier,
            champion,
        )
        if result is not None:
            results.append(result)
    return results
