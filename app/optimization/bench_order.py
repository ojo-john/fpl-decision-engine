"""FPL-accurate bench ordering: maps 4 bench candidates onto real bench slots [12, 13, 14, 15].

Real FPL bench mechanics (see design notes cited in the originating task):
slot 12 is a fixed reserve-goalkeeper slot — it can ONLY be subbed in for the
starting goalkeeper (never for an outfield player), and is always a
straight, unconditional swap if the starting GK blanked and the reserve GK
played. Slots 13/14/15 are the 3 outfield subs, tried in that priority order
for each blanking outfield starter; the first sub who played AND whose
swap-in keeps the resulting XI's per-position counts within
``[squad_min_play, squad_max_play]`` is used.

Plain descending-projected-points bench ordering is provably wrong here: bench
slot order determines who "wins" contention for a blank when multiple bench
players could legally fill it. Worked example (formation 5-4-1: DEF=5 at max,
MID=4, FWD=1 at min; rules DEF 3-5/MID 2-5/FWD 1-3; bench outfield =
MID_Y(8pts), FWD_Z1(10pts), FWD_Z2(1pt)): if one DEF starter and the sole FWD
starter both blank simultaneously, plain points-descending order
(13=FWD_Z1, 14=MID_Y, 15=FWD_Z2) has the DEF blank consume FWD_Z1 first
(tried first, legal for a DEF blank), leaving only FWD_Z2(1pt) legal for the
FWD blank (MID_Y is illegal there — subbing a MID into the sole FWD slot
violates FWD's min=1) — total sub value 10+1=11, and MID_Y (8pts) never comes
on despite being fully eligible for the DEF blank. A **breadth-ascending**
order instead ranks by how many outfield position-classes a candidate could
legally fill (narrowest/most-constrained first, points as tiebreak): here
13=MID_Y(breadth 1), 14=FWD_Z1(breadth 2), 15=FWD_Z2 — DEF blank now takes
MID_Y (8pts, its only legal use case), FWD blank takes FWD_Z1 (10pts) — total
18, strictly better and the true optimal assignment. This is a deterministic,
mechanical, most-constrained-first scheduling heuristic — no minutes/
probability modeling needed.

KNOWN LIMITATION: this optimizes bench-slot contention correctly, but cannot
account for the starting XI's OWN internal processing order (which of several
simultaneously-blanking starters gets its autosub resolved first in the real
FPL engine) since that order isn't modeled or persisted anywhere in this
schema. Out of scope here — would need either persisting an explicit XI slot
order or probabilistic modeling (Milestone 3+).
"""

from app.optimization.candidates import OptimizationCandidate
from app.optimization.constraints import PositionRequirement

_GKP_SHORT_NAME = "GKP"


def breadth(
    candidate: OptimizationCandidate,
    starting_position_counts: dict[str, int],
    position_requirements: dict[str, PositionRequirement],
) -> int:
    """Count how many outfield position-classes this bench candidate could legally
    fill if that class's starter blanks — i.e., how many outfield positions this
    player is a legal (formation-respecting) autosub for. GKP is excluded (handled
    separately via the dedicated reserve-GK slot, never contended).
    """
    n = 0
    for short_name in position_requirements:
        if short_name == _GKP_SHORT_NAME:
            continue
        counts = dict(starting_position_counts)
        counts[short_name] = counts.get(short_name, 0) - 1
        counts[candidate.position_short_name] = counts.get(candidate.position_short_name, 0) + 1
        if all(
            req.squad_min_play <= counts.get(other_short_name, 0) <= req.squad_max_play
            for other_short_name, req in position_requirements.items()
            if other_short_name != _GKP_SHORT_NAME
        ):
            n += 1
    return n


def order_bench(
    bench_candidates: list[OptimizationCandidate],
    starting_position_counts: dict[str, int],
    position_requirements: dict[str, PositionRequirement],
) -> list[OptimizationCandidate]:
    """Return the 4 bench candidates in real FPL bench-slot order [12, 13, 14, 15].

    Slot 12 is always the reserve goalkeeper (there is exactly one non-starting
    GKP in any legal squad, given squad_select=2/squad_min_play=squad_max_play=1
    for GKP — no decision needed). Slots 13-15 are the 3 outfield bench players,
    ordered by ascending ``breadth`` (narrowest/most-constrained-eligibility
    first, then descending projected_points, then descending now_cost, then
    player_id string for full determinism) — see the module docstring for the
    worked example proving plain points-descending order is provably
    suboptimal versus this most-constrained-first ordering.

    The GKP-slot carve-out only applies when ``"GKP"`` is actually a modeled
    position (i.e. present in ``position_requirements``) — real FPL squads
    always have this. When it isn't present (e.g. a trivial/padded
    constraint shape used by solver-mechanics unit tests with no notion of a
    goalkeeper position), every bench candidate is treated uniformly and
    ordered by ascending breadth alone, with no fixed-size assumption.
    """
    if _GKP_SHORT_NAME not in position_requirements:
        return sorted(
            bench_candidates,
            key=lambda c: (
                breadth(c, starting_position_counts, position_requirements),
                -c.projected_points,
                -c.now_cost,
                str(c.player_id),
            ),
        )

    gk_bench = [c for c in bench_candidates if c.position_short_name == _GKP_SHORT_NAME]
    assert len(gk_bench) == 1, "expected exactly one non-starting GKP in a legal squad"
    outfield = [c for c in bench_candidates if c.position_short_name != _GKP_SHORT_NAME]
    assert len(outfield) == 3, "expected exactly 3 outfield bench players in a legal squad"

    outfield_sorted = sorted(
        outfield,
        key=lambda c: (
            breadth(c, starting_position_counts, position_requirements),
            -c.projected_points,
            -c.now_cost,
            str(c.player_id),
        ),
    )
    return [gk_bench[0], *outfield_sorted]
