"""Pure Monte Carlo matchup simulator (Milestone 5 Layer 3: H2H Strategy).

No I/O, no DB, no ORM objects — every input here is a plain, frozen
dataclass (mirrors ``app.h2h.rival_state``'s own "pure computation module"
discipline), so ``simulate_matchup`` is trivially unit-testable with
hand-built Python objects. All DB querying (resolving starting XIs,
projecting points via ``app.optimization.projection.project_player_with_champion``,
collecting empirical residuals from ``player_gameweek_stats``) is the
caller's job (``app.jobs.handlers.h2h_simulation``) — this module only turns
already-fetched data into a ``SimulationResult``.

Why Monte Carlo, not closed-form/normal: individual FPL player scores are far
from normally distributed (rotation risk creates a fat mass at/near zero,
captain doubling stretches the tail asymmetrically, and scoring is discrete/
bounded below). A normal approximation would misrepresent exactly the tails
that matter at the single-digit margins typical of real H2H matchups — so
every player-gameweek is instead resolved by an empirical-residual bootstrap:
sample a historical ``(actual_total_points - projected_points)`` residual
and add it to this gameweek's point estimate, per draw.

Scope (deliberately bounded, not silently missing):

- This module always simulates exactly the 11-player list it's handed — it
  has no concept of a bench itself. ``app.jobs.handlers.h2h_simulation``
  (the only real caller) DOES now resolve a simplified real-FPL autosub
  before calling in (``_resolve_effective_starting_xi``): a starting player
  hard-excluded from playing is replaced by the highest-priority ELIGIBLE
  bench player of the same broad class (GK-for-GK, any-outfield-for-any-
  outfield), for both sides, with the user's captain armband transferring to
  the vice-captain exactly once if the original captain is subbed out. That
  caller-side autosub does NOT enforce full per-position formation legality
  (e.g. never fewer than 3 defenders) — a same-class swap is correct for the
  overwhelming majority of real single-injury cases, not guaranteed for
  unusual multi-injury combinations. It also cannot rescue a squad whose
  bench is itself exhausted/all-ineligible, or a rival's stochastically
  sampled transfer-IN target (drawn from Layer 2's
  ``predicted_likely_transfer_targets``, never itself eligibility-filtered)
  — in both of those residual cases, this module still samples residual
  noise around a ``projected_points=0.0`` floor for that draw rather than
  hard-zeroing it, which can slightly overstate that player's ceiling.
- Multi-transfer draws swap the top-2 DISTINCT weakest positions Layer 2
  tracked (``app.jobs.handlers.league_sync._build_transfer_target_candidates``
  now identifies up to 2 weak positions, not just 1) — falling back to
  repeating the single tracked position if Layer 2 only ever found
  candidates for one (thin squad data). This is still an approximation of a
  real 2-transfer plan (Layer 2 has no notion of a coordinated 2-move
  strategy, just 2 independent weak-spot signals), not a full plan search.
- Chip usage for the TARGET gameweek is never modeled/predicted for either
  side (no wildcard/bench-boost/triple-captain simulation) — the user side
  applies whatever multiplier its own actual pick record carries (which
  *does* capture a real triple-captain if the caller resolved one from
  ``raw_picks_json``), but the rival side always uses a plain 2x captain
  multiplier (``_RIVAL_CAPTAIN_MULTIPLIER``) regardless of the rival's own
  remaining-chip inventory. This is a deliberate, NOT-fixed limitation:
  Layer 2 (``app.h2h.rival_state``) only derives a remaining-chip
  INVENTORY, never a probability of USING a specific chip on a specific
  future gameweek — fabricating such a probability here with no real
  evidence behind it would be exactly the kind of falsely-precise estimate
  this codebase's confidence-handling philosophy exists to avoid (see
  ``packages.domain.h2h.RivalStateConfidence``'s own docstring). Modeling
  this properly belongs in Layer 2 (a genuine chip-USE prediction, with its
  own honestly-earned confidence), not as an invented heuristic here.

Randomness: every entry point here takes an injected ``random.Random``
instance and never touches the global ``random``/``numpy.random`` module
functions -- the first place in this codebase needing randomness, so this
sets the precedent tests rely on (seed the injected ``Random`` for
deterministic, reproducible unit tests).
"""

import random
import statistics
from dataclasses import dataclass, field

from packages.domain.h2h import RivalStateConfidence

SIMULATION_METHOD_VERSION = 1

# 5,000 paired (user, rival) draws -- large enough to resolve single-digit
# win/draw/loss probability differences to within roughly +/-1.4pp (95% CI
# for a Bernoulli proportion at n=5000), small enough to stay comfortably
# inside one bounded job run.
SAMPLE_COUNT = 5_000

# A player's OWN historical residual sample is used once at least this many
# observations exist; below that, a single freak gameweek could dominate the
# sampled distribution, so the (richer, but less player-specific) same-
# position pool is used instead. Same "don't overclaim from a thin sample"
# spirit as app.h2h.rival_state's own confidence thresholds.
MIN_PLAYER_SPECIFIC_RESIDUALS = 3

# Real captain multiplier (2x); triple-captain (3x) is a chip-use decision
# this layer never predicts for the rival side -- see module docstring.
_RIVAL_CAPTAIN_MULTIPLIER = 2

_FLOOR_PERCENTILE = 5.0
_CEILING_PERCENTILE = 95.0

_TransferCategory = str  # "no_transfer" | "one_transfer" | "multi_transfer"


@dataclass(frozen=True)
class UserPlayerInput:
    """One starting-XI player on the user's side, for the target gameweek."""

    player_id: str
    projected_points: float
    # The pick's own multiplier for this gameweek: 1 (normal), 2 (captain),
    # or 3 (triple captain) -- read verbatim from the resolved squad's pick
    # record, applied deterministically (never sampled).
    multiplier: int
    own_residuals: tuple[float, ...] = ()
    position_residuals: tuple[float, ...] = ()


@dataclass(frozen=True)
class RivalHeldPlayerInput:
    """One starting-XI player from the rival's last-known squad."""

    player_id: str
    # Raw FPL element id, used to match RivalProfile.predicted_captain_probabilities'
    # keys -- None if this player couldn't be identified by element id (should
    # not normally happen for a held player, but degrades gracefully).
    fpl_element_id: int | None
    position: str | None
    projected_points: float
    own_residuals: tuple[float, ...] = ()
    position_residuals: tuple[float, ...] = ()


@dataclass(frozen=True)
class RivalTransferTargetInput:
    """One ranked candidate replacement player (RivalProfile.predicted_likely_transfer_targets)."""

    player_id: str
    position: str | None
    projected_points: float
    own_residuals: tuple[float, ...] = ()
    position_residuals: tuple[float, ...] = ()


@dataclass(frozen=True)
class SimulationInputs:
    user_players: tuple[UserPlayerInput, ...]
    rival_held_players: tuple[RivalHeldPlayerInput, ...]
    rival_transfer_targets: tuple[RivalTransferTargetInput, ...] = ()
    rival_no_transfer_probability: float = 1.0
    rival_one_transfer_probability: float = 0.0
    rival_multi_transfer_probability: float = 0.0
    # Keyed by raw FPL element id, as a string (RivalProfile's own JSON shape).
    rival_captain_probabilities: dict[str, float] = field(default_factory=dict)
    rival_state_confidence: RivalStateConfidence = RivalStateConfidence.LOW
    sample_count: int = SAMPLE_COUNT


@dataclass(frozen=True)
class SimulationResult:
    """Summary statistics only -- never the raw per-draw sample arrays."""

    sample_count: int
    user_score_mean: float
    user_score_floor: float
    user_score_ceiling: float
    rival_score_mean: float
    rival_score_floor: float
    rival_score_ceiling: float
    win_probability: float
    draw_probability: float
    loss_probability: float
    expected_margin: float
    margin_std_dev: float
    confidence: RivalStateConfidence


@dataclass(frozen=True)
class _DrawSquadMember:
    fpl_element_id: int | None
    projected_points: float
    own_residuals: tuple[float, ...]
    position_residuals: tuple[float, ...]


def _sample_residual(
    rng: random.Random, own_residuals: tuple[float, ...], position_residuals: tuple[float, ...]
) -> float:
    """Sample one empirical residual, falling back from player-specific to position-pooled to zero.

    Zero (pure projection, no noise) is the final, honestly-degraded
    fallback when NEITHER sample exists at all -- a genuinely cold system
    with no historical player_gameweek_stats yet, rather than raising or
    fabricating a distribution.
    """
    if len(own_residuals) >= MIN_PLAYER_SPECIFIC_RESIDUALS:
        return rng.choice(own_residuals)
    if position_residuals:
        return rng.choice(position_residuals)
    if own_residuals:
        return rng.choice(own_residuals)
    return 0.0


def _drawn_score(rng: random.Random, projected_points: float, own_residuals, position_residuals) -> int:
    """One player's simulated total_points for this draw: projection + residual, rounded to the nearest int.

    Rounded because real FPL ``total_points`` is always an integer -- summing
    un-rounded floats would make exact-score draws (a real, common H2H
    outcome) nearly impossible to ever land on.
    """
    residual = _sample_residual(rng, own_residuals, position_residuals)
    return round(projected_points + residual)


def _simulate_user_draw(rng: random.Random, user_players: tuple[UserPlayerInput, ...]) -> int:
    return sum(
        _drawn_score(rng, p.projected_points, p.own_residuals, p.position_residuals) * p.multiplier
        for p in user_players
    )


def _sample_transfer_category(
    rng: random.Random, no_p: float, one_p: float, multi_p: float
) -> _TransferCategory:
    weights = [max(no_p, 0.0), max(one_p, 0.0), max(multi_p, 0.0)]
    if sum(weights) <= 0:
        # Defensive fallback only -- app.h2h.rival_state always produces
        # probabilities summing to ~1 (real distribution or population
        # prior). Assume the squad is held rather than fabricate transfers.
        return "no_transfer"
    return rng.choices(["no_transfer", "one_transfer", "multi_transfer"], weights=weights, k=1)[0]


def _weighted_choice(
    rng: random.Random, targets: tuple[RivalTransferTargetInput, ...]
) -> RivalTransferTargetInput:
    """Rank-weighted sample: predicted_likely_transfer_targets is already ranked most- to least-likely.

    Weight ~ 1/(rank+1) (harmonic decay) rather than uniform -- a documented
    judgment call: the ranking itself is evidence of relative likelihood, so
    the top candidate should be sampled more often than the fifth, but never
    to the point of being treated as certain.
    """
    weights = [1.0 / (index + 1) for index in range(len(targets))]
    return rng.choices(targets, weights=weights, k=1)[0]


def _ranked_target_positions(targets: tuple[RivalTransferTargetInput, ...]) -> list[str | None]:
    """Distinct target positions in Layer 2's own ranked order (first-appearance order).

    Layer 2 (``app.jobs.handlers.league_sync._build_transfer_target_candidates``) interleaves
    candidates from its top-2 tracked weakest positions, weakest-first, so the first-seen distinct
    positions here are already the rival's weakest-to-least-weak squad spots.
    """
    seen: list[str | None] = []
    for target in targets:
        if target.position not in seen:
            seen.append(target.position)
    return seen


def _select_out_index(
    squad: list[_DrawSquadMember],
    positions: list[str | None],
    target_position: str | None,
    excluded: set[int],
) -> int:
    """Pick ONE squad index to transfer OUT: the weakest-projected player at ``target_position``
    not already claimed by an earlier swap this draw. Falls back to the weakest-projected player
    overall (any position, still excluding already-claimed indices) if none of that position remain.
    """
    by_position = sorted(
        (i for i in range(len(squad)) if positions[i] == target_position and i not in excluded),
        key=lambda i: squad[i].projected_points,
    )
    if by_position:
        return by_position[0]
    remaining = sorted(
        (i for i in range(len(squad)) if i not in excluded), key=lambda i: squad[i].projected_points
    )
    return remaining[0]


def _simulate_rival_draw(
    rng: random.Random,
    held: tuple[RivalHeldPlayerInput, ...],
    targets: tuple[RivalTransferTargetInput, ...],
    no_p: float,
    one_p: float,
    multi_p: float,
    captain_probabilities: dict[str, float],
) -> int:
    squad = [
        _DrawSquadMember(h.fpl_element_id, h.projected_points, h.own_residuals, h.position_residuals)
        for h in held
    ]
    positions: list[str | None] = [h.position for h in held]

    if targets:
        category = _sample_transfer_category(rng, no_p, one_p, multi_p)
        swap_count = {"no_transfer": 0, "one_transfer": 1, "multi_transfer": 2}[category]
        if swap_count:
            # Multi-transfer swaps the top-2 DISTINCT weakest positions Layer 2 tracked (see
            # _ranked_target_positions) -- falls back to repeating the single weakest position if
            # Layer 2 only ever found candidates for one (thin data), never fabricating a second.
            positions_ranked = _ranked_target_positions(targets)
            positions_to_fill = positions_ranked[:swap_count]
            while len(positions_to_fill) < swap_count and positions_ranked:
                positions_to_fill.append(positions_ranked[-1])

            used_indices: set[int] = set()
            for target_position in positions_to_fill[: len(squad)]:
                idx = _select_out_index(squad, positions, target_position, used_indices)
                used_indices.add(idx)
                position_targets = tuple(t for t in targets if t.position == target_position) or targets
                chosen = _weighted_choice(rng, position_targets)
                squad[idx] = _DrawSquadMember(
                    fpl_element_id=None,  # a transferred-in player was never observed as this rival's captain
                    projected_points=chosen.projected_points,
                    own_residuals=chosen.own_residuals,
                    position_residuals=chosen.position_residuals,
                )
                positions[idx] = chosen.position

    scores = [_drawn_score(rng, m.projected_points, m.own_residuals, m.position_residuals) for m in squad]

    captain_index: int | None = None
    if captain_probabilities:
        element_ids = list(captain_probabilities.keys())
        weights = list(captain_probabilities.values())
        sampled_element_id = rng.choices(element_ids, weights=weights, k=1)[0]
        for i, member in enumerate(squad):
            if member.fpl_element_id is not None and str(member.fpl_element_id) == sampled_element_id:
                captain_index = i
                break
    if captain_index is None:
        # Cold-start fallback (empty predicted_captain_probabilities) AND the
        # "sampled captain no longer in this draw's squad" case (transferred
        # out, or a candidate id that never matched a held player) share the
        # same honest fallback: captain whoever scored highest THIS draw.
        captain_index = max(range(len(scores)), key=lambda i: scores[i])

    return sum(
        score * (_RIVAL_CAPTAIN_MULTIPLIER if i == captain_index else 1) for i, score in enumerate(scores)
    )


def _percentile(sorted_values: list[int], pct: float) -> float:
    """Linear-interpolation percentile, hand-rolled to avoid a ``statistics.quantiles`` method mismatch."""
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = (pct / 100.0) * (len(sorted_values) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = rank - lower
    return sorted_values[lower] + fraction * (sorted_values[upper] - sorted_values[lower])


def _resolve_confidence(rival_state_confidence: RivalStateConfidence) -> RivalStateConfidence:
    """Hard-floor at the input RivalProfile's own confidence.

    Layer 3 introduces no independent confidence signal of its own yet (a
    tight simulated win probability is only ever as trustworthy as the
    rival-behavior predictions feeding it) -- so this is a direct passthrough
    today, not an average or an optimistic blend. Kept as its own named
    function (rather than inlined) so a future layer can add a genuine
    second signal (e.g. residual-sample sufficiency) and floor against it,
    without touching every call site.
    """
    return rival_state_confidence


def sample_rival_scores(inputs: SimulationInputs, rng: random.Random) -> tuple[int, ...]:
    """Draw ``inputs.sample_count`` independent rival-side total scores.

    Milestone 5 Layer 4 (``app.jobs.handlers.h2h_simulation``'s multi-candidate
    strategy comparison) calls this exactly ONCE per H2H_SIMULATION run and
    reuses the resulting tuple across every one of the 7 candidate
    simulations — the rival's own predicted state (``RivalProfile``) never
    changes within one run regardless of which of the user's OWN squad
    variants is being compared, so re-sampling it per candidate would only
    add noise, never fairness. This is what guarantees an apples-to-apples
    comparison: every candidate's ``win_probability`` differs ONLY because of
    that candidate's own user-side score distribution, never because it drew
    a luckier/unluckier rival sample.
    """
    return tuple(
        _simulate_rival_draw(
            rng,
            inputs.rival_held_players,
            inputs.rival_transfer_targets,
            inputs.rival_no_transfer_probability,
            inputs.rival_one_transfer_probability,
            inputs.rival_multi_transfer_probability,
            inputs.rival_captain_probabilities,
        )
        for _ in range(inputs.sample_count)
    )


def sample_user_scores(
    user_players: tuple[UserPlayerInput, ...], rng: random.Random, sample_count: int
) -> tuple[int, ...]:
    """Draw ``sample_count`` independent user-side total scores for one candidate squad/XI/captain."""
    return tuple(_simulate_user_draw(rng, user_players) for _ in range(sample_count))


def summarize_matchup(
    user_scores: tuple[int, ...], rival_scores: tuple[int, ...], confidence: RivalStateConfidence
) -> SimulationResult:
    """Turn one (user_scores, rival_scores) paired-draw pair into a full ``SimulationResult``.

    Extracted from ``simulate_matchup``'s own tail so Layer 4 can summarize
    several different user-side candidates against one shared, precomputed
    ``rival_scores`` sequence (see ``sample_rival_scores``) without
    duplicating this statistics logic.
    """
    n = len(user_scores)
    wins = sum(1 for u, r in zip(user_scores, rival_scores, strict=True) if u > r)
    draws = sum(1 for u, r in zip(user_scores, rival_scores, strict=True) if u == r)
    losses = n - wins - draws
    margins = [u - r for u, r in zip(user_scores, rival_scores, strict=True)]

    sorted_user = sorted(user_scores)
    sorted_rival = sorted(rival_scores)

    return SimulationResult(
        sample_count=n,
        user_score_mean=statistics.fmean(user_scores),
        user_score_floor=_percentile(sorted_user, _FLOOR_PERCENTILE),
        user_score_ceiling=_percentile(sorted_user, _CEILING_PERCENTILE),
        rival_score_mean=statistics.fmean(rival_scores),
        rival_score_floor=_percentile(sorted_rival, _FLOOR_PERCENTILE),
        rival_score_ceiling=_percentile(sorted_rival, _CEILING_PERCENTILE),
        win_probability=wins / n,
        draw_probability=draws / n,
        loss_probability=losses / n,
        expected_margin=statistics.fmean(margins),
        margin_std_dev=statistics.pstdev(margins) if len(margins) > 1 else 0.0,
        confidence=_resolve_confidence(confidence),
    )


def simulate_matchup(inputs: SimulationInputs, rng: random.Random) -> SimulationResult:
    """Run the full paired Monte Carlo simulation and summarize it. Never raises for empty-but-valid inputs.

    ``rng`` must be injected by the caller (never the global ``random``
    module) so tests can seed it for deterministic, reproducible assertions.
    """
    user_scores: list[int] = []
    rival_scores: list[int] = []
    for _ in range(inputs.sample_count):
        user_scores.append(_simulate_user_draw(rng, inputs.user_players))
        rival_scores.append(
            _simulate_rival_draw(
                rng,
                inputs.rival_held_players,
                inputs.rival_transfer_targets,
                inputs.rival_no_transfer_probability,
                inputs.rival_one_transfer_probability,
                inputs.rival_multi_transfer_probability,
                inputs.rival_captain_probabilities,
            )
        )

    n = inputs.sample_count
    wins = sum(1 for u, r in zip(user_scores, rival_scores, strict=True) if u > r)
    draws = sum(1 for u, r in zip(user_scores, rival_scores, strict=True) if u == r)
    losses = n - wins - draws
    margins = [u - r for u, r in zip(user_scores, rival_scores, strict=True)]

    sorted_user = sorted(user_scores)
    sorted_rival = sorted(rival_scores)

    return SimulationResult(
        sample_count=n,
        user_score_mean=statistics.fmean(user_scores),
        user_score_floor=_percentile(sorted_user, _FLOOR_PERCENTILE),
        user_score_ceiling=_percentile(sorted_user, _CEILING_PERCENTILE),
        rival_score_mean=statistics.fmean(rival_scores),
        rival_score_floor=_percentile(sorted_rival, _FLOOR_PERCENTILE),
        rival_score_ceiling=_percentile(sorted_rival, _CEILING_PERCENTILE),
        win_probability=wins / n,
        draw_probability=draws / n,
        loss_probability=losses / n,
        expected_margin=statistics.fmean(margins),
        margin_std_dev=statistics.pstdev(margins) if len(margins) > 1 else 0.0,
        confidence=_resolve_confidence(inputs.rival_state_confidence),
    )
