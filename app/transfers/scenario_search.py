"""Pure 0/1/2-transfer scenario search over already-built ``TransferCandidate`` pools.

No DB/session access anywhere in this module — every input (squad
candidates, position-bucketed IN shortlists, selling prices, budget/team
constraints) is already-computed plain data, matching the "solver internals
operate on plain dataclasses, no ORM objects" convention set by
``app.optimization.joint_squad_solver``/``lineup_solver``.

Computational bound (2-transfer search): 15 squad members give
``C(15, 2) = 105`` candidate OUT pairs; for each, the IN-pair space is at
most ``IN_CANDIDATE_SHORTLIST_SIZE**2 = 625`` (differing OUT positions) or
``C(IN_CANDIDATE_SHORTLIST_SIZE, 2) = 300`` (same OUT position) — roughly
``105 * 625 ~= 65,625`` pure-arithmetic evaluations in the worst case, with
zero DB/model calls per evaluation (``horizon_points``/``now_cost``/selling
price are all already-computed plain values by this point, precomputed
once outside this module). That's comfortably fast in plain Python (tens of
milliseconds at most), so this module deliberately uses a naive nested loop
rather than a pruning/branch-and-bound algorithm — there is no evidence a
fancier algorithm is warranted for this milestone's stated "start simple"
scope.
"""

import itertools
import uuid
from collections import Counter
from dataclasses import dataclass

from app.transfers.candidates import TransferCandidate


class InvalidScenarioSearchInputError(ValueError):
    """Raised when ``evaluate_transfer_scenarios`` is called with input that violates its contract.

    Fails fast at this function's own boundary rather than letting a
    malformed caller input surface as an opaque ``KeyError`` deep inside a
    search loop — see the specific checks in ``evaluate_transfer_scenarios``
    for what's validated and why.
    """


@dataclass(frozen=True)
class TransferMove:
    player_out: TransferCandidate
    player_in: TransferCandidate
    points_gain: float  # player_in.horizon_points - player_out.horizon_points


@dataclass(frozen=True)
class TransferScenario:
    transfers_count: int  # 0, 1, or 2
    moves: list[TransferMove]  # empty for transfers_count == 0, or if no legal move exists
    raw_horizon_points_gain: float
    hit_cost: int  # points lost to hits, as a positive number
    net_horizon_points_gain: float  # raw_horizon_points_gain - hit_cost


def _hit_cost(transfers_count: int, free_transfers_available: int, transfer_hit_cost_magnitude: int) -> int:
    return max(0, transfers_count - free_transfers_available) * transfer_hit_cost_magnitude


def _team_counts(candidates: list[TransferCandidate]) -> Counter[uuid.UUID]:
    return Counter(candidate.team_id for candidate in candidates)


def _swap_is_team_legal(
    base_counts: Counter[uuid.UUID],
    outs: list[TransferCandidate],
    ins: list[TransferCandidate],
    team_limit: int,
) -> bool:
    """Whether swapping ``outs`` for ``ins`` keeps every club's count within ``team_limit``.

    Checked against the FULL resulting squad composition in one shot — never
    via independent per-swap checks, which can each individually pass while
    the combination as a whole violates the real joint constraint (e.g. two
    incoming players from the same club that together push that club over
    the limit, even though neither swap alone would).
    """
    counts = base_counts.copy()
    counts.subtract(out.team_id for out in outs)
    counts.update(in_candidate.team_id for in_candidate in ins)
    return all(count <= team_limit for count in counts.values())


def _best_one_transfer_moves(
    squad_candidates: list[TransferCandidate],
    in_shortlists_by_position: dict[str, list[TransferCandidate]],
    selling_prices: dict[uuid.UUID, int],
    team_limit: int,
    bank: int,
    base_team_counts: Counter[uuid.UUID],
) -> tuple[list[TransferMove], float]:
    """Exhaustive best single (out, in) swap by ``points_gain``, respecting budget/team-limit legality.

    Returns ``([], 0.0)`` if no legal swap exists at all.
    """
    best_moves: list[TransferMove] = []
    best_gain = 0.0
    found = False

    for out in squad_candidates:
        for candidate_in in in_shortlists_by_position.get(out.position_short_name, []):
            if candidate_in.player_id == out.player_id:
                continue
            if candidate_in.now_cost > selling_prices[out.player_id] + bank:
                continue
            if not _swap_is_team_legal(base_team_counts, [out], [candidate_in], team_limit):
                continue

            points_gain = candidate_in.horizon_points - out.horizon_points
            if not found or points_gain > best_gain:
                found = True
                best_gain = points_gain
                best_moves = [TransferMove(player_out=out, player_in=candidate_in, points_gain=points_gain)]

    if not found:
        return [], 0.0
    return best_moves, best_gain


def _in_pair_candidates(
    position_a: str,
    position_b: str,
    in_shortlists_by_position: dict[str, list[TransferCandidate]],
) -> list[tuple[TransferCandidate, TransferCandidate]]:
    """All distinct 2-player IN combinations legal for a given pair of OUT positions.

    Same position: every distinct 2-combination from that position's
    shortlist — order doesn't matter, since both incoming players join the
    squad together (no specific incoming player needs to be "assigned" to a
    specific outgoing slot). Different positions: the full Cartesian product
    of the two positions' shortlists — each incoming player's position is
    fixed, so the position-for-position assignment is forced, not a choice.
    """
    if position_a == position_b:
        shortlist = in_shortlists_by_position.get(position_a, [])
        return list(itertools.combinations(shortlist, 2))
    shortlist_a = in_shortlists_by_position.get(position_a, [])
    shortlist_b = in_shortlists_by_position.get(position_b, [])
    return [(a, b) for a in shortlist_a for b in shortlist_b]


def _best_two_transfer_moves(
    squad_candidates: list[TransferCandidate],
    in_shortlists_by_position: dict[str, list[TransferCandidate]],
    selling_prices: dict[uuid.UUID, int],
    team_limit: int,
    bank: int,
    base_team_counts: Counter[uuid.UUID],
) -> tuple[list[TransferMove], float]:
    """Exhaustive best pair-of-pairs 2-transfer search: JOINT budget + JOINT team-limit legality.

    See module docstring for the computational bound this naive nested loop
    stays within. Returns ``([], 0.0)`` if no legal 2-transfer combination
    exists.
    """
    best_moves: list[TransferMove] = []
    best_gain = 0.0
    found = False

    # _in_pair_candidates' result depends only on the (position_a, position_b)
    # pair, of which a real squad (4 distinct positions) has very few ordered
    # combinations -- cached here so it's built at most once per distinct
    # position pair rather than once per one of the up-to-105 OUT pairs.
    in_pairs_by_position_pair: dict[tuple[str, str], list[tuple[TransferCandidate, TransferCandidate]]] = {}

    for out_a, out_b in itertools.combinations(squad_candidates, 2):
        position_pair = (out_a.position_short_name, out_b.position_short_name)
        if position_pair not in in_pairs_by_position_pair:
            in_pairs_by_position_pair[position_pair] = _in_pair_candidates(
                out_a.position_short_name, out_b.position_short_name, in_shortlists_by_position
            )
        in_pairs = in_pairs_by_position_pair[position_pair]
        for in_a, in_b in in_pairs:
            if in_a.player_id == in_b.player_id:
                continue
            owned_ids = {out_a.player_id, out_b.player_id}
            if in_a.player_id in owned_ids or in_b.player_id in owned_ids:
                continue

            combined_cost = in_a.now_cost + in_b.now_cost
            combined_selling = selling_prices[out_a.player_id] + selling_prices[out_b.player_id]
            if combined_cost > combined_selling + bank:
                continue
            if not _swap_is_team_legal(base_team_counts, [out_a, out_b], [in_a, in_b], team_limit):
                continue

            points_gain = (in_a.horizon_points + in_b.horizon_points) - (
                out_a.horizon_points + out_b.horizon_points
            )
            if not found or points_gain > best_gain:
                found = True
                best_gain = points_gain
                best_moves = [
                    TransferMove(
                        player_out=out_a,
                        player_in=in_a,
                        points_gain=in_a.horizon_points - out_a.horizon_points,
                    ),
                    TransferMove(
                        player_out=out_b,
                        player_in=in_b,
                        points_gain=in_b.horizon_points - out_b.horizon_points,
                    ),
                ]

    if not found:
        return [], 0.0
    return best_moves, best_gain


def evaluate_transfer_scenarios(
    squad_candidates: list[TransferCandidate],
    in_shortlists_by_position: dict[str, list[TransferCandidate]],
    selling_prices: dict[uuid.UUID, int],
    team_limit: int,
    bank: int,
    free_transfers_available: int,
    transfer_hit_cost_magnitude: int,
) -> tuple[dict[int, TransferScenario], TransferScenario]:
    """Evaluate the ROLL/0-transfer, best 1-transfer, and best 2-transfer scenarios.

    Returns ``({0: scenario, 1: scenario, 2: scenario}, recommended_scenario)``.

    0-transfer (ROLL): ``raw_horizon_points_gain=0.0``, ``hit_cost=0`` always.

    1-transfer: exhaustive search over all (out, in) pairs where
    ``in.position_short_name == out.position_short_name``,
    ``in.now_cost <= selling_prices[out.player_id] + bank``, and the team
    composition after the swap doesn't exceed ``team_limit`` for any club.
    Best pair by ``points_gain`` wins. If no legal pair exists at all, the
    1-transfer scenario degrades to ``moves=[]``,
    ``raw_horizon_points_gain=0.0`` — a graceful "no real transfer available"
    signal that (correctly) never outranks ROLL once hit cost is applied.

    2-transfer: exhaustive search over all (out_pair, in_pair) combinations
    satisfying: position-for-position match between the two out/in pairs
    (order doesn't matter — OUT-DEF+OUT-MID pairs with IN-DEF+IN-MID in
    either assignment), JOINT budget (sum of both incoming costs <= sum of
    both outgoing selling prices + bank), and JOINT team-limit legality
    checked against the FULL resulting 15-player squad, not each swap
    independently — two incoming players from the same new club can jointly
    breach ``team_limit`` even when neither swap alone would. Best
    pair-of-pairs by summed ``points_gain`` wins; same graceful ``moves=[]``
    degradation as the 1-transfer search if no legal combination exists.

    Hit cost for scenario N = ``max(0, N - free_transfers_available) *
    transfer_hit_cost_magnitude``. ``net_horizon_points_gain =
    raw_horizon_points_gain - hit_cost``.

    Recommended scenario = the one with the highest ``net_horizon_points_gain``;
    ties broken toward FEWER transfers (a transfer is never recommended "for
    free" when an equally-good cheaper option exists — a transfer must
    clear its true cost, never win on being merely equal to it).

    Preconditions (validated at this boundary; raises
    ``InvalidScenarioSearchInputError`` rather than failing deep inside a
    search loop with an opaque ``KeyError``):

    - ``transfer_hit_cost_magnitude`` must be non-negative. Layer 1's
      ``app.transfers.rules.TransferRules.transfer_hit_cost`` is stored
      SIGNED (e.g. ``-4``) — callers must pass ``abs(rules.transfer_hit_cost)``
      here, not the raw signed value, or every hit would silently turn into a
      points BONUS instead of a cost.
    - ``selling_prices`` must contain an entry for every player in
      ``squad_candidates`` — every squad member is a potential OUT candidate,
      so every one of them needs a priced exit.

    ``squad_candidates`` is also assumed to represent the manager's COMPLETE
    squad (all 15 members, not a filtered subset) — ``_team_counts`` derives
    each club's current count from exactly this list, so silently omitting a
    squad member here would undercount their club and could let a swap
    breach ``team_limit`` in the real squad without this function ever
    seeing it. This one isn't runtime-checked (this function has no
    independent way to know the "real" squad size), so it's the caller's
    responsibility.
    """
    if transfer_hit_cost_magnitude < 0:
        raise InvalidScenarioSearchInputError(
            "transfer_hit_cost_magnitude must be a non-negative magnitude, got "
            f"{transfer_hit_cost_magnitude}. app.transfers.rules.TransferRules.transfer_hit_cost is "
            "stored SIGNED (e.g. -4) -- pass abs(rules.transfer_hit_cost), not the raw signed value."
        )

    missing_selling_prices = [c.player_id for c in squad_candidates if c.player_id not in selling_prices]
    if missing_selling_prices:
        raise InvalidScenarioSearchInputError(
            f"selling_prices is missing an entry for {len(missing_selling_prices)} squad candidate(s), "
            f"e.g. {missing_selling_prices[0]}. Every squad_candidates player is a potential OUT "
            "candidate and needs a selling price -- see app.transfers.selling_price."
        )

    base_team_counts = _team_counts(squad_candidates)

    scenario0 = TransferScenario(
        transfers_count=0, moves=[], raw_horizon_points_gain=0.0, hit_cost=0, net_horizon_points_gain=0.0
    )

    one_moves, one_raw_gain = _best_one_transfer_moves(
        squad_candidates, in_shortlists_by_position, selling_prices, team_limit, bank, base_team_counts
    )
    one_hit_cost = _hit_cost(1, free_transfers_available, transfer_hit_cost_magnitude)
    scenario1 = TransferScenario(
        transfers_count=1,
        moves=one_moves,
        raw_horizon_points_gain=one_raw_gain,
        hit_cost=one_hit_cost,
        net_horizon_points_gain=one_raw_gain - one_hit_cost,
    )

    two_moves, two_raw_gain = _best_two_transfer_moves(
        squad_candidates, in_shortlists_by_position, selling_prices, team_limit, bank, base_team_counts
    )
    two_hit_cost = _hit_cost(2, free_transfers_available, transfer_hit_cost_magnitude)
    scenario2 = TransferScenario(
        transfers_count=2,
        moves=two_moves,
        raw_horizon_points_gain=two_raw_gain,
        hit_cost=two_hit_cost,
        net_horizon_points_gain=two_raw_gain - two_hit_cost,
    )

    scenarios = {0: scenario0, 1: scenario1, 2: scenario2}
    recommended = min(scenarios.values(), key=lambda s: (-s.net_horizon_points_gain, s.transfers_count))
    return scenarios, recommended
