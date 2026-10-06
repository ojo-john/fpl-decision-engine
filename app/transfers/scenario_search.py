"""MIP-based 0..N-transfer scenario search over already-built ``TransferCandidate`` pools.

No DB/session access anywhere in this module — every input (squad
candidates, position-bucketed IN shortlists, selling prices, budget/team
constraints) is already-computed plain data, matching the "solver internals
operate on plain dataclasses, no ORM objects" convention set by
``app.optimization.joint_squad_solver``/``lineup_solver``.

Real gap closed 2026-10-06: this module used to hardcode exactly 0/1/2-transfer
scenarios via brute-force nested loops, regardless of how many free transfers
a manager actually had banked (FPL allows rolling up to 5). A manager with 4
free transfers would never see a 3- or 4-transfer scenario at all, even a
free, beneficial one — not because it was evaluated and rejected, but because
the search space never considered it.

Naive brute-force doesn't generalize past 2 transfers: 15 squad members give
``C(15, 2) = 105`` candidate OUT pairs for a 2-transfer search (what the old
nested-loop approach did, in ``~105 * 625 ~= 65,625`` evaluations — fine in
plain Python). The SAME approach for 3 transfers is ``C(15, 3) = 455`` OUT
triples against an IN space up to ``IN_CANDIDATE_SHORTLIST_SIZE**3 ~= 15,625``
— around 7.1 million evaluations. For 4 transfers it's ``C(15, 4) = 1365``
against up to ``25**4`` — on the order of half a billion. Neither is viable
as a naive nested loop.

The fix: a MIP solve per transfer count, maximizing total squad
``horizon_points`` subject to squad-size/position-shape/team-limit/budget
legality and "exactly K current-squad players replaced" — the exact same
``pulp``/CBC approach this codebase already uses for the much larger Wildcard
squad-selection problem (``app.optimization.joint_squad_solver``), which is
proven fast at a scale well beyond what a single-digit-K transfer search
needs. This is globally optimal for each K (not a greedy approximation), and
naturally subsumes the old 1/2-transfer logic as special cases (K=1, K=2) —
verified to produce identical results on every existing test case.
"""

import uuid
from dataclasses import dataclass

import pulp

from app.optimization.solver_common import map_solver_status
from app.transfers.candidates import TransferCandidate
from packages.domain.optimization import SolverStatus

# FPL's real rule: free transfers bank up to a maximum of 5. Evaluating beyond this would be
# evaluating a scenario no real manager could ever actually hold, free or otherwise -- a hard,
# principled ceiling rather than an arbitrary one.
MAX_TRANSFERS_TO_EVALUATE_CEILING = 5

# Preserves this module's pre-2026-10-06 behavior exactly for every manager with 2 or fewer free
# transfers (the overwhelmingly common case) -- always show at least the "what would a hit cost
# me" 2-transfer option, same as before this change, even at 0 or 1 free transfers.
_MIN_TRANSFERS_TO_EVALUATE = 2


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
    transfers_count: int  # 0 through min(max(free_transfers_available, 2), MAX_TRANSFERS_TO_EVALUATE_CEILING)
    moves: list[TransferMove]  # empty for transfers_count == 0, or if no legal move exists
    raw_horizon_points_gain: float
    hit_cost: int  # points lost to hits, as a positive number
    net_horizon_points_gain: float  # raw_horizon_points_gain - hit_cost


def _hit_cost(transfers_count: int, free_transfers_available: int, transfer_hit_cost_magnitude: int) -> int:
    return max(0, transfers_count - free_transfers_available) * transfer_hit_cost_magnitude


def _position_counts(squad_candidates: list[TransferCandidate]) -> dict[str, int]:
    """Per-position counts in the current squad — the new squad must match these exactly.

    FPL's squad-shape rule (2 GKP / 5 DEF / 5 MID / 3 FWD) is implicit here rather than a
    hardcoded constant: the current squad is itself already a legal squad, so its own real
    per-position counts ARE those required counts.
    """
    counts: dict[str, int] = {}
    for candidate in squad_candidates:
        counts[candidate.position_short_name] = counts.get(candidate.position_short_name, 0) + 1
    return counts


def _pair_moves_by_position(
    outs: list[TransferCandidate], ins: list[TransferCandidate]
) -> list[TransferMove]:
    """Pairs outgoing/incoming candidates by position for move-level reporting.

    The MIP's per-position equality constraints (see ``_best_k_transfer_moves``) guarantee the
    number of outgoing players at each position exactly equals the number of incoming players at
    that same position — so this pairing always has a match for every candidate, by construction.
    Which specific same-position out is paired with which same-position in is cosmetic (the total
    points gain is identical regardless of pairing within a position), matching the old
    2-transfer code's own "joint legality, arbitrary internal pairing" precedent.
    """
    ins_by_position: dict[str, list[TransferCandidate]] = {}
    for candidate in ins:
        ins_by_position.setdefault(candidate.position_short_name, []).append(candidate)

    moves: list[TransferMove] = []
    for out in outs:
        in_candidate = ins_by_position[out.position_short_name].pop()
        moves.append(
            TransferMove(
                player_out=out,
                player_in=in_candidate,
                points_gain=in_candidate.horizon_points - out.horizon_points,
            )
        )
    return moves


def _best_k_transfer_moves(
    squad_candidates: list[TransferCandidate],
    in_shortlists_by_position: dict[str, list[TransferCandidate]],
    selling_prices: dict[uuid.UUID, int],
    team_limit: int,
    bank: int,
    k: int,
) -> tuple[list[TransferMove], float]:
    """Best legal K-transfer swap via MIP: maximize incoming-minus-outgoing ``horizon_points``.

    Constraints: squad size preserved, per-position counts preserved (see
    ``_position_counts``), no club exceeds ``team_limit`` in the resulting squad, incoming
    (non-owned) cost doesn't exceed outgoing (dropped) selling price plus ``bank``, and exactly
    ``k`` current squad members are replaced.

    Returns ``([], 0.0)`` for ``k == 0``, or if no legal K-transfer combination exists (the MIP
    reports infeasible) — mirrors the old per-N functions' own "no real transfer available"
    graceful degradation, which correctly never outranks ROLL once hit cost is applied.
    """
    if k == 0:
        return [], 0.0

    owned_ids = {candidate.player_id for candidate in squad_candidates}
    in_pool: dict[uuid.UUID, TransferCandidate] = {}
    for candidates in in_shortlists_by_position.values():
        for candidate in candidates:
            if candidate.player_id not in owned_ids:
                in_pool[candidate.player_id] = candidate

    all_candidates: dict[uuid.UUID, TransferCandidate] = {c.player_id: c for c in squad_candidates}
    all_candidates.update(in_pool)

    problem = pulp.LpProblem(f"transfer_{k}_scenario", pulp.LpMaximize)
    x = {player_id: pulp.LpVariable(f"x_{player_id.hex}", cat="Binary") for player_id in all_candidates}

    problem += pulp.lpSum(
        all_candidates[player_id].horizon_points * x[player_id] for player_id in all_candidates
    )

    problem += pulp.lpSum(x.values()) == len(squad_candidates)

    for position, required_count in _position_counts(squad_candidates).items():
        problem += (
            pulp.lpSum(
                x[player_id]
                for player_id, candidate in all_candidates.items()
                if candidate.position_short_name == position
            )
            == required_count
        )

    team_ids = {candidate.team_id for candidate in all_candidates.values()}
    for team_id in team_ids:
        problem += (
            pulp.lpSum(
                x[player_id]
                for player_id, candidate in all_candidates.items()
                if candidate.team_id == team_id
            )
            <= team_limit
        )

    problem += (
        pulp.lpSum(all_candidates[player_id].now_cost * x[player_id] for player_id in in_pool)
        <= pulp.lpSum(selling_prices[player_id] * (1 - x[player_id]) for player_id in owned_ids) + bank
    )

    problem += pulp.lpSum(1 - x[player_id] for player_id in owned_ids) == k

    problem.solve(pulp.PULP_CBC_CMD(msg=False))
    if map_solver_status(problem.status) != SolverStatus.OPTIMAL:
        return [], 0.0

    selected_ids = {player_id for player_id, var in x.items() if var.value() == 1}
    outs = [
        candidate
        for player_id, candidate in all_candidates.items()
        if player_id in owned_ids and player_id not in selected_ids
    ]
    ins = [
        candidate
        for player_id, candidate in all_candidates.items()
        if player_id not in owned_ids and player_id in selected_ids
    ]
    moves = _pair_moves_by_position(outs, ins)
    raw_gain = sum(move.points_gain for move in moves)
    return moves, raw_gain


def _max_transfers_to_evaluate(free_transfers_available: int) -> int:
    return min(max(free_transfers_available, _MIN_TRANSFERS_TO_EVALUATE), MAX_TRANSFERS_TO_EVALUATE_CEILING)


def evaluate_transfer_scenarios(
    squad_candidates: list[TransferCandidate],
    in_shortlists_by_position: dict[str, list[TransferCandidate]],
    selling_prices: dict[uuid.UUID, int],
    team_limit: int,
    bank: int,
    free_transfers_available: int,
    transfer_hit_cost_magnitude: int,
) -> tuple[dict[int, TransferScenario], TransferScenario]:
    """Evaluate the ROLL/0-transfer scenario plus the best 1..N-transfer scenarios.

    ``N = min(max(free_transfers_available, 2), MAX_TRANSFERS_TO_EVALUATE_CEILING)`` — always at
    least 2 (so a manager with 0 or 1 free transfers still sees what taking a hit would cost,
    matching this module's behavior before 2026-10-06), extended up to how many free transfers
    are actually banked, capped at FPL's real 5-transfer rollover maximum.

    Returns ``({0: scenario, 1: scenario, ..., N: scenario}, recommended_scenario)``.

    0-transfer (ROLL): ``raw_horizon_points_gain=0.0``, ``hit_cost=0`` always.

    Each K-transfer (K >= 1) scenario is the GLOBALLY OPTIMAL legal K-player swap by summed
    ``points_gain``, found via ``_best_k_transfer_moves`` — see that function for the exact
    legality constraints (position-shape/team-limit/budget). Degrades gracefully to
    ``moves=[]``, ``raw_horizon_points_gain=0.0`` if no legal K-transfer combination exists.

    Hit cost for scenario K = ``max(0, K - free_transfers_available) *
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
    squad (all 15 members, not a filtered subset) — ``_position_counts`` derives
    the required per-position shape from exactly this list, so silently omitting a
    squad member here would misrepresent that shape and could let a swap
    legal against the wrong shape through. This one isn't runtime-checked (this
    function has no independent way to know the "real" squad size), so it's the
    caller's responsibility.
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

    scenarios: dict[int, TransferScenario] = {
        0: TransferScenario(
            transfers_count=0, moves=[], raw_horizon_points_gain=0.0, hit_cost=0, net_horizon_points_gain=0.0
        )
    }

    for k in range(1, _max_transfers_to_evaluate(free_transfers_available) + 1):
        moves, raw_gain = _best_k_transfer_moves(
            squad_candidates, in_shortlists_by_position, selling_prices, team_limit, bank, k
        )
        hit_cost = _hit_cost(k, free_transfers_available, transfer_hit_cost_magnitude)
        scenarios[k] = TransferScenario(
            transfers_count=k,
            moves=moves,
            raw_horizon_points_gain=raw_gain,
            hit_cost=hit_cost,
            net_horizon_points_gain=raw_gain - hit_cost,
        )

    recommended = min(scenarios.values(), key=lambda s: (-s.net_horizon_points_gain, s.transfers_count))
    return scenarios, recommended
