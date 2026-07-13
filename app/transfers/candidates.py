"""Transfer-candidate pool construction: OUT (current squad) and IN (shortlisted) candidates.

Mirrors ``app.optimization.candidates.OptimizationCandidate``'s shape and
conventions, but adds ``horizon_points`` (summed across a multi-gameweek
horizon, not a single gameweek) and surfaces ``is_eligible`` explicitly on
every candidate, matching ``PlayerProjection.is_eligible`` (see
``app.optimization.projection``).

Performance note (IN-candidate pool, ~700 non-owned players across all
positions): computing the full horizon-summed projection
(``compute_horizon_points``, ``app.transfers.horizon_projection``) for every
one of those ~700 players would mean ``horizon_length`` calls to
``project_player_with_champion`` PER PLAYER — when a champion model is
loaded, each of those calls issues its own DB feature-row query, so that's
plausibly several thousand DB round trips before any shortlisting even
happens. This is a read-heavy, no-ML-training operation, so a modest number
of extra calls is not a real concern — but several thousand is enough to be
worth avoiding cheaply. ``build_in_shortlists`` therefore uses a two-stage
approach: first, a cheap SINGLE-gameweek projection (one
``project_player_with_champion`` call per player, against only the
horizon's first gameweek — the same order of cost
``SQUAD_OPTIMIZATION``'s own candidate-building already pays today) ranks
the whole pool and cuts it down to ``_GW1_PREFILTER_SIZE`` per position;
only THEN does the much smaller prefiltered set (~4 positions *
``_GW1_PREFILTER_SIZE`` players) pay the full ``horizon_length``-multiplied
cost of ``compute_horizon_points``. This bounds the expensive part of this
function to a small, roughly constant number of calls regardless of how
large the total player pool grows — a deliberate, justified optimization,
not a premature one.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.optimization.champion_loader import LoadedChampion
from app.optimization.projection import project_player, project_player_with_champion
from app.transfers.horizon_projection import compute_horizon_points
from packages.database.models.player import Player
from packages.database.models.position import Position

IN_CANDIDATE_SHORTLIST_SIZE = 25

# Generous single-gameweek prefilter width per position — wide enough that a
# genuine top-IN_CANDIDATE_SHORTLIST_SIZE horizon target essentially never
# gets squeezed out just for ranking outside the top 50 on gameweek-1 alone.
# See module docstring for why this two-stage approach exists at all.
_GW1_PREFILTER_SIZE = 50


@dataclass(frozen=True)
class TransferCandidate:
    player_id: uuid.UUID
    position_short_name: str
    team_id: uuid.UUID
    now_cost: int
    horizon_points: float
    is_eligible: bool


@dataclass(frozen=True)
class SquadPlayerInfo:
    """Plain, DB-free view of one current-squad member's static, solver-relevant attributes.

    ``build_out_candidates`` deliberately takes no ``Session`` — it is pure
    and fully testable with plain in-memory data (see
    ``tests/unit/test_transfer_candidates.py``). The caller (Layer 3's job
    handler, not built in this layer) is responsible for resolving each
    squad player's ``position_short_name``/``team_id``/``now_cost`` via the
    same ``Player``/``Position`` join
    ``app.jobs.handlers.squad_optimization._build_candidates`` already
    performs, and building this dict once up front.
    """

    position_short_name: str
    team_id: uuid.UUID
    now_cost: int


def build_out_candidates(
    squad_player_ids: list[uuid.UUID],
    players_by_id: dict[uuid.UUID, SquadPlayerInfo],
    horizon_points_by_id: dict[uuid.UUID, float],
) -> list[TransferCandidate]:
    """Wrap all 15 current squad members as OUT candidates — no reduction needed, cheap.

    ``is_eligible`` is always ``True`` for OUT candidates: a squad member can
    always legitimately be transferred OUT regardless of their current
    availability status (an injured player is often exactly who you'd want
    to transfer out) — unlike IN candidates, where hard-excluded players are
    never worth bringing in and are filtered out before shortlisting (see
    ``build_in_shortlists``).
    """
    return [
        TransferCandidate(
            player_id=player_id,
            position_short_name=players_by_id[player_id].position_short_name,
            team_id=players_by_id[player_id].team_id,
            now_cost=players_by_id[player_id].now_cost,
            horizon_points=horizon_points_by_id[player_id],
            is_eligible=True,
        )
        for player_id in squad_player_ids
    ]


def top_k_candidates_by_position(
    candidates: list[TransferCandidate], k: int
) -> dict[str, list[TransferCandidate]]:
    """Pure ranking/reduction step: group by ``position_short_name``, sort each group descending
    by ``horizon_points`` (ties broken by ``player_id`` string for full reproducibility), and
    keep only the top ``k`` per position.

    Factored out from ``build_in_shortlists`` so the actual "reduce to a
    shortlist" policy is testable without a DB — mirrors this codebase's
    general pure-computation/DB-orchestration split (e.g.
    ``app.features.engineering`` vs ``app.features.service``).
    """
    by_position: dict[str, list[TransferCandidate]] = {}
    for candidate in candidates:
        by_position.setdefault(candidate.position_short_name, []).append(candidate)

    return {
        position: sorted(group, key=lambda c: (-c.horizon_points, str(c.player_id)))[:k]
        for position, group in by_position.items()
    }


def build_in_shortlists(
    session: Session,
    season_id: uuid.UUID,
    excluded_player_ids: set[uuid.UUID],
    horizon_gameweek_ids: list[uuid.UUID],
    champion: LoadedChampion | None,
) -> dict[str, list[TransferCandidate]]:
    """Per position (GKP/DEF/MID/FWD), the top ``IN_CANDIDATE_SHORTLIST_SIZE`` non-owned,
    non-quarantined, eligible players by ``horizon_points`` descending.

    Bounds the IN space from ~700 players to ~100 before any combinatorial
    search — a genuinely excellent replacement is essentially always near
    the top of its position's projected-points ranking over the horizon; a
    player ranked ~80th at their position is not going to be the single best
    transfer target even accounting for budget/team-limit interaction. This
    is explicitly a bounded heuristic, not a claim of global optimality —
    see the module docstring for the two-stage prefilter used to keep the
    cost of computing this ranking itself bounded.

    Returns an empty dict if ``horizon_gameweek_ids`` is empty (nothing to
    project against — the real off-season state, not an error; see
    ``app.optimization.gameweek_resolution.resolve_horizon_gameweeks``). A
    position with zero eligible non-owned players simply has no key in the
    returned dict — callers (``app.transfers.scenario_search``) already
    treat "no candidates for this position" as "no legal swap exists there",
    not an error.
    """
    if not horizon_gameweek_ids:
        return {}

    players = (
        session.execute(select(Player).where(Player.season_id == season_id, Player.is_quarantined.is_(False)))
        .scalars()
        .all()
    )
    positions_by_id = {
        p.id: p for p in session.execute(select(Position).where(Position.season_id == season_id)).scalars()
    }

    first_gameweek_id = horizon_gameweek_ids[0]

    # Stage 1: cheap single-gameweek prefilter over the whole non-owned pool.
    ranked_by_position: dict[str, list[tuple[float, Player]]] = {}
    for player in players:
        if player.id in excluded_player_ids:
            continue
        if player.team_id is None or player.position_id is None:
            continue
        position = positions_by_id.get(player.position_id)
        if position is None:
            continue
        # Hard-exclusion (i/s/u) is a static property of player.status alone
        # -- identical for every gameweek project_player_with_champion would
        # ever check (see that function's own "hard-excluded statuses
        # short-circuit FIRST" ordering). Using the cheap, DB-free
        # project_player() here is exact, not an approximation -- and avoids
        # reimplementing the exclusion rule or paying for a champion-aware
        # call just to throw its result away.
        if not project_player(player).is_eligible:
            continue
        gw1_points = project_player_with_champion(
            session, player, first_gameweek_id, champion
        ).projected_points
        ranked_by_position.setdefault(position.short_name, []).append((gw1_points, player))

    prefiltered_players: list[tuple[Player, str]] = []
    for position_short_name, ranked in ranked_by_position.items():
        ranked.sort(key=lambda item: (-item[0], str(item[1].id)))
        prefiltered_players.extend(
            (player, position_short_name) for _, player in ranked[:_GW1_PREFILTER_SIZE]
        )

    # Stage 2: full horizon sum, only for the prefiltered set.
    candidates: list[TransferCandidate] = []
    for player, position_short_name in prefiltered_players:
        assert player.team_id is not None  # narrowed above; for mypy only
        horizon_points = compute_horizon_points(session, player, horizon_gameweek_ids, champion)
        candidates.append(
            TransferCandidate(
                player_id=player.id,
                position_short_name=position_short_name,
                team_id=player.team_id,
                now_cost=player.now_cost,
                horizon_points=horizon_points,
                is_eligible=True,
            )
        )

    return top_k_candidates_by_position(candidates, IN_CANDIDATE_SHORTLIST_SIZE)
