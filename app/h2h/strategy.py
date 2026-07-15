"""Pure risk-adjusted strategy-candidate generation/selection (Milestone 5 Layer 4: H2H Strategy).

No I/O, no DB, no ORM objects — every input here is a plain, frozen
dataclass or a ``UserPlayerInput``/``H2HStrategyLabel`` already defined
elsewhere, matching ``app.h2h.simulate``/``app.h2h.rival_state``'s own "pure
computation module" discipline. All DB querying (resolving the user's
starting XI/bench, a prior ``TransferOptimizationRun``'s scenarios/moves,
each starter's ``chance_of_playing_next_round``/``selected_by_percent``) is
the caller's job (``app.jobs.handlers.h2h_simulation``) — this module only
turns already-fetched data into a captain choice, a modified picks list, or a
final recommendation.

This is the final piece of master spec section 20 ("Head-to-Head Strategy"):
comparing 7 named candidates for one upcoming matchup and recommending one.
Every candidate is ultimately scored by running (a variant of) the same
Monte Carlo matchup simulator (``app.h2h.simulate.simulate_matchup``) against
the SAME precomputed rival-side score distribution — see
``app.jobs.handlers.h2h_simulation`` for how fairness across candidates is
guaranteed (the rival distribution is sampled once per run and reused, never
re-randomized per candidate).

Judgment calls made here, documented rather than left implicit:

- **LOW_VARIANCE/HIGH_UPSIDE re-rank CAPTAIN choice, not squad membership.**
  The master spec describes these as "a thin re-scoring layer, not a new
  projection system" — re-running Milestone 4's whole transfer-scenario
  search with a variance-adjusted objective (instead of raw horizon points)
  would be exactly the "new projection system" that phrase rules out, and
  would duplicate ``app.transfers.scenario_search`` wholesale rather than
  reusing it. Re-picking the captain from the EXISTING starting XI by a
  variance-adjusted score is the smallest change that genuinely embodies
  "low variance" (favor a reliable floor over a marginally higher mean) and
  "high upside" (favor ceiling over mean) risk profiles, and cleanly reuses
  every other candidate's squad/XI resolution.
- **Variance-adjusted score** = ``projected_points + sign * VARIANCE_ADJUSTMENT_K
  * stdev(residuals)``, where ``sign`` is -1 for LOW_VARIANCE (penalize
  spread) and +1 for HIGH_UPSIDE (reward spread), and the residual sample
  used mirrors ``app.h2h.simulate._sample_residual``'s own fallback order
  (player-specific if at least ``MIN_PLAYER_SPECIFIC_RESIDUALS`` observed,
  else the position-pooled sample, else zero variance). ``VARIANCE_ADJUSTMENT_K
  = 1.0`` is a documented, hand-picked constant (one standard deviation of
  swing) — not fit against any data, same spirit as
  ``app.h2h.rival_state``'s own hand-picked population priors.
- **CAPTAIN_COVER's "floor"** = ``Player.chance_of_playing_next_round``, the
  same appearance-risk field ``app.optimization.projection.project_player``
  already uses to down-weight a doubtful starter's projection — the most
  direct, real evidence of rotation/appearance risk this codebase has, in
  contrast to the residual-variance-based "floor" LOW_VARIANCE uses. A
  ``None`` value (no doubt at all recorded) is treated as 100 (fully nailed
  on), matching ``project_player``'s own "unscaled" reading of an absent
  value.
- **DIFFERENTIAL_CAPTAIN's ownership cut** = strictly below the STARTING XI's
  OWN median ``selected_by_percent`` (a squad-relative differential, not a
  whole-league threshold this module has no visibility into); a missing
  ``selected_by_percent`` is treated as 0.0 (unknown ownership assumed
  differential-friendly, never assumed template). Among that lower-owned
  half, the highest-ceiling starter (HIGH_UPSIDE's own scoring, reused) is
  captained. Falls back to the full starter list if literally none qualifies
  (e.g. a 1-starter edge case, or every starter tied on ownership).
"""

import statistics
import uuid
from dataclasses import dataclass

from app.h2h.simulate import MIN_PLAYER_SPECIFIC_RESIDUALS, UserPlayerInput
from packages.domain.h2h import H2HStrategyLabel

# One standard deviation of a player's own empirical (actual - projected)
# residual spread -- see module docstring.
VARIANCE_ADJUSTMENT_K = 1.0

# CAPTAIN_COVER considers exactly the top-2 highest-floor starters, per the
# master spec's own wording ("top-2 highest-floor... starters").
_CAPTAIN_COVER_POOL_SIZE = 2

# chance_of_playing_next_round reading when the field is NULL (no recorded
# doubt at all) -- mirrors app.optimization.projection.project_player's own
# "absent means fully fit" convention.
_ASSUMED_FULLY_FIT_CHANCE = 100

# Declaration order doubles as the "least changed from EP_OPTIMAL" tie-break
# ordering -- see select_recommended_candidate's own docstring.
_STRATEGY_DECLARATION_ORDER = list(H2HStrategyLabel)


@dataclass(frozen=True)
class StarterCandidate:
    """One EP_OPTIMAL starting-XI player, with the extra fields captain-strategy re-ranking needs
    beyond what ``UserPlayerInput`` itself carries (``app.h2h.simulate`` has no reason to know about
    ownership/appearance-risk -- those are strategy-comparison-only concerns).
    """

    player_id: uuid.UUID
    projected_points: float
    own_residuals: tuple[float, ...]
    position_residuals: tuple[float, ...]
    chance_of_playing_next_round: int | None
    selected_by_percent: float | None


@dataclass(frozen=True)
class CandidateResult:
    """One strategy candidate's simulated outcome, ready to persist as a ``H2HStrategyCandidate`` row."""

    strategy_label: H2HStrategyLabel
    win_probability: float
    draw_probability: float
    loss_probability: float
    expected_margin: float
    transfer_optimization_run_scenario_id: uuid.UUID | None = None
    captain_player_id: uuid.UUID | None = None


def _residual_sample(candidate: StarterCandidate) -> tuple[float, ...]:
    """Mirrors ``app.h2h.simulate._sample_residual``'s own fallback order, but returns the whole
    sample (for a stdev), not one drawn value.
    """
    if len(candidate.own_residuals) >= MIN_PLAYER_SPECIFIC_RESIDUALS:
        return candidate.own_residuals
    if candidate.position_residuals:
        return candidate.position_residuals
    return candidate.own_residuals


def _residual_stdev(candidate: StarterCandidate) -> float:
    sample = _residual_sample(candidate)
    return statistics.pstdev(sample) if len(sample) > 1 else 0.0


def variance_adjusted_score(candidate: StarterCandidate, sign: int) -> float:
    """``projected_points`` shifted by ``sign * VARIANCE_ADJUSTMENT_K`` standard deviations of this
    player's own empirical residual spread. ``sign=-1`` for a low-variance (safe-floor) read,
    ``sign=+1`` for a high-upside (ceiling) read.
    """
    return candidate.projected_points + sign * VARIANCE_ADJUSTMENT_K * _residual_stdev(candidate)


def _tie_break_sort_key(candidate: StarterCandidate, score: float) -> tuple[float, str]:
    """Descending by ``score``, then ascending by ``player_id`` string for full determinism
    (mirrors ``app.optimization.lineup_solver.rank_key``'s own tie-break convention).
    """
    return (-score, str(candidate.player_id))


def select_low_variance_captain(starters: list[StarterCandidate]) -> uuid.UUID:
    """Captain = the starter maximizing ``mean - k*std`` (favor a reliable floor over raw mean)."""
    return min(starters, key=lambda c: _tie_break_sort_key(c, variance_adjusted_score(c, sign=-1))).player_id


def select_high_upside_captain(starters: list[StarterCandidate]) -> uuid.UUID:
    """Captain = the starter maximizing ``mean + k*std`` (favor ceiling over raw mean)."""
    return min(starters, key=lambda c: _tie_break_sort_key(c, variance_adjusted_score(c, sign=1))).player_id


def _floor_rank_key(candidate: StarterCandidate) -> tuple[int, float, str]:
    chance = (
        candidate.chance_of_playing_next_round
        if candidate.chance_of_playing_next_round is not None
        else _ASSUMED_FULLY_FIT_CHANCE
    )
    return (-chance, -candidate.projected_points, str(candidate.player_id))


def select_captain_cover_captain(starters: list[StarterCandidate]) -> uuid.UUID:
    """Captain = whichever of the top-2 highest-floor (least rotation/appearance-risk) starters has
    the higher projection -- a hedge against captaining the single highest-projected starter when a
    nearly-as-good alternative is meaningfully safer to actually start.
    """
    floor_ranked = sorted(starters, key=_floor_rank_key)
    pool = floor_ranked[:_CAPTAIN_COVER_POOL_SIZE]
    return min(pool, key=lambda c: _tie_break_sort_key(c, c.projected_points)).player_id


def select_differential_captain(starters: list[StarterCandidate]) -> uuid.UUID:
    """Captain = the highest-ceiling starter among the squad's own lower-owned half. See module
    docstring for the exact ownership-cut and fallback rules.
    """
    ownership = [c.selected_by_percent if c.selected_by_percent is not None else 0.0 for c in starters]
    median_ownership = statistics.median(ownership)
    pool = [
        c
        for c in starters
        if (c.selected_by_percent if c.selected_by_percent is not None else 0.0) < median_ownership
    ]
    if not pool:
        pool = starters
    return min(pool, key=lambda c: _tie_break_sort_key(c, variance_adjusted_score(c, sign=1))).player_id


@dataclass(frozen=True)
class PickSwap:
    player_out_element_id: int
    player_in_element_id: int


def apply_pick_swaps(picks: list[dict], swaps: list[PickSwap]) -> list[dict]:
    """Return a NEW picks list (never mutates the input) with each swap's OUT element replaced by its
    IN element, preserving whichever slot (position/multiplier/is_captain/is_vice_captain) the
    outgoing player held.

    The incoming replacement therefore inherits an armband if the outgoing player held one -- a
    deliberate simplification: Milestone 4's own scenario search has no notion of captain
    re-assignment post-transfer, so re-running a full captain re-selection here is out of scope for
    this reuse (documented, not a silent gap; the caller still re-runs autosub resolution against the
    new squad, since the incoming player could themselves be unavailable).
    """
    by_out_element = {swap.player_out_element_id: swap.player_in_element_id for swap in swaps}
    return [
        {**pick, "element": by_out_element[pick["element"]]}
        if pick["element"] in by_out_element
        else dict(pick)
        for pick in picks
    ]


def recaptain(
    user_players: tuple[UserPlayerInput, ...], new_captain_player_id: str
) -> tuple[UserPlayerInput, ...]:
    """Move whatever armband multiplier (2 for a normal captain, 3 for a triple-captain chip)
    currently exists onto a DIFFERENT starter, leaving every other player's multiplier at 1.

    If no player currently carries a multiplier > 1 (the armband was lost entirely -- both captain
    and vice unavailable, see ``app.jobs.handlers.h2h_simulation``'s own autosub docstring), there is
    no armband to reassign and the input is returned unchanged: a captain-alternative strategy has
    nothing meaningful to differ on when there's no armband multiplier in play at all.
    """
    armband_multiplier = max((p.multiplier for p in user_players), default=1)
    if armband_multiplier <= 1:
        return user_players
    return tuple(
        UserPlayerInput(
            player_id=p.player_id,
            projected_points=p.projected_points,
            multiplier=armband_multiplier if p.player_id == new_captain_player_id else 1,
            own_residuals=p.own_residuals,
            position_residuals=p.position_residuals,
        )
        for p in user_players
    )


def final_captain_player_id(user_players: tuple[UserPlayerInput, ...]) -> str | None:
    """Whichever player currently carries a multiplier > 1, or ``None`` if the armband was lost
    (see ``recaptain``'s own docstring).
    """
    captained = [p for p in user_players if p.multiplier > 1]
    return captained[0].player_id if captained else None


_NO_TRANSFER_STRATEGY_LABELS = {
    H2HStrategyLabel.EP_OPTIMAL,
    H2HStrategyLabel.LOW_VARIANCE,
    H2HStrategyLabel.HIGH_UPSIDE,
    H2HStrategyLabel.CAPTAIN_COVER,
    H2HStrategyLabel.DIFFERENTIAL_CAPTAIN,
    H2HStrategyLabel.NO_HIT,
}


def _recommendation_tie_break_key(candidate: CandidateResult) -> tuple[float, int, int, int]:
    return (
        -candidate.win_probability,
        0 if candidate.strategy_label == H2HStrategyLabel.EP_OPTIMAL else 1,
        0 if candidate.strategy_label in _NO_TRANSFER_STRATEGY_LABELS else 1,
        _STRATEGY_DECLARATION_ORDER.index(candidate.strategy_label),
    )


def select_recommended_candidate(candidates: list[CandidateResult]) -> CandidateResult:
    """Pick the recommended candidate: highest ``win_probability`` wins; ties broken (in order) by
    (1) preferring EP_OPTIMAL itself -- a change is never recommended "for free" when it does no
    better than doing nothing different, (2) preferring a no-transfer candidate over a transfer one
    (CALCULATED_HIT is the only strategy that ever changes squad membership) -- a transfer must
    clear a strictly higher win probability to be worth its real-world hit-cost/timing risk, not
    merely tie it, and (3) this module's own ``H2HStrategyLabel`` declaration order, for full
    determinism when every other tie-break is exhausted.
    """
    return min(candidates, key=_recommendation_tie_break_key)


def h2h_adjustment_materially_changed(recommended: CandidateResult) -> bool:
    """Whether the recommended candidate differs from the plain overall-points-optimal plan
    (master spec section 20's own required disclosure).
    """
    return recommended.strategy_label != H2HStrategyLabel.EP_OPTIMAL
