"""Pure squad-composition risk signals over an already-solved squad.

``solve_joint_squad_and_xi`` maximizes total projected points subject to
budget/formation/team-limit constraints -- it has zero notion of downside
risk, positional depth, or "how much of my budget rides on one player."
These signals were built after a live example: a preview squad spent
GBP15.5m (15.5% of the whole budget) on one striker, and a manual re-solve
with that player excluded from candidates found an alternative squad scoring
only ~1.3 points less over a 6-gameweek horizon -- but that alternative
dropped to a single genuine starting forward with near-zero-projected bench
cover. Both facts required a one-off manual Python-shell re-solve to
discover. This module makes them visible on every preview automatically,
without a second solve.
"""

import uuid
from dataclasses import dataclass

from app.optimization.candidates import OptimizationCandidate
from app.optimization.constraints import OptimizationConstraints

# A squad member's cost share is flagged "notably concentrated" once it
# exceeds this multiple of an EVENLY-SPREAD per-member share of the budget
# (total_spend_limit / squad_size). Deliberately relative to the constraints
# actually in force, not a hardcoded FPL-specific percentage (e.g. "15%") --
# stays meaningful if squad size or the total budget rules ever change. 1.5x
# is a modest bar: most real squads have SOME premium spend above the flat-
# even share by design (that's the whole point of a budget curve across
# positions), so this only fires when one player's share is meaningfully
# more than that, not merely "more than average."
_CONCENTRATION_MULTIPLIER_THRESHOLD = 1.5

# A position's bench cover only counts as a real structural risk when there
# is exactly ONE starter at that position (a true single point of failure --
# e.g. a single-forward formation) AND the best bench option projects below
# this fraction of that one starter's own points, over the same horizon.
# Positions with 2+ starters are deliberately never flagged here: losing one
# starter among several is a normal, already-priced-in FPL risk every
# manager accepts, not a structural weakness worth surfacing. The 0.25
# fraction is relative to the starter's own projection (not an absolute
# point value), which keeps this scale-invariant across projection formula
# versions and horizon lengths.
_BENCH_COVER_RATIO_THRESHOLD = 0.25


@dataclass(frozen=True)
class BudgetConcentration:
    most_expensive_player_id: uuid.UUID
    most_expensive_player_cost: int
    budget_share_pct: float
    is_notably_concentrated: bool


def compute_budget_concentration(
    squad_members: list[OptimizationCandidate], constraints: OptimizationConstraints
) -> BudgetConcentration | None:
    """The single most expensive squad member's cost as a share of the total budget.

    Returns ``None`` for an empty squad (nothing to report -- e.g. an
    infeasible solve with zero members) rather than a fabricated zero value.
    """
    if not squad_members:
        return None
    top = max(squad_members, key=lambda c: c.now_cost)
    budget_share_pct = (top.now_cost / constraints.total_spend_limit) * 100
    even_share_pct = 100 / constraints.squad_size
    return BudgetConcentration(
        most_expensive_player_id=top.player_id,
        most_expensive_player_cost=top.now_cost,
        budget_share_pct=round(budget_share_pct, 1),
        is_notably_concentrated=budget_share_pct >= even_share_pct * _CONCENTRATION_MULTIPLIER_THRESHOLD,
    )


def compute_bench_depth_warnings(
    starters: list[OptimizationCandidate],
    bench: list[OptimizationCandidate],
    player_names: dict[uuid.UUID, str],
) -> list[str]:
    """Human-readable warnings for positions with a genuine single point of failure.

    Only ever evaluates positions with exactly one starter -- see module
    docstring for why positions with 2+ starters are deliberately skipped.
    Empty list if nothing at any single-starter position is notably weak.
    """
    warnings: list[str] = []
    positions = sorted({c.position_short_name for c in starters})
    for position in positions:
        position_starters = [c for c in starters if c.position_short_name == position]
        if len(position_starters) != 1:
            continue
        starter = position_starters[0]
        if starter.projected_points <= 0:
            continue

        position_bench = [c for c in bench if c.position_short_name == position]
        starter_name = player_names.get(starter.player_id, "this player")

        if not position_bench:
            warnings.append(
                f"{position}: {starter_name} is your only starting option and there is no bench "
                "cover at all at this position -- an injury or rotation here leaves the slot empty."
            )
            continue

        best_bench = max(position_bench, key=lambda c: c.projected_points)
        cover_ratio = best_bench.projected_points / starter.projected_points
        if cover_ratio < _BENCH_COVER_RATIO_THRESHOLD:
            bench_name = player_names.get(best_bench.player_id, "your bench cover")
            warnings.append(
                f"{position}: {starter_name} is your only starting option -- bench cover "
                f"({bench_name}) projects far lower, so an injury or rotation here would cost real points."
            )
    return warnings
