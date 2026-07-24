"""H2H_SIMULATION job: Monte Carlo-simulate one user-vs-rival matchup for a target gameweek.

Milestone 5 Layer 3 — wires Layer 1 (``ManagerSquadSnapshot``: the user's own
last-known squad), Layer 2 (``RivalProfile``: the rival's derived/predicted
state), and Milestone 3 Layer 4's ``project_player_with_champion`` together
into ``app.h2h.simulate.simulate_matchup``, then persists the result.

Milestone 5 Layer 4 (H2H Strategy: risk-adjusted strategy comparison — the
final piece of master spec section 20) extends this SAME job type (not a new
one, mirroring how Layer 2 extended ``LEAGUE_SYNC``): after the Layer 3
EP_OPTIMAL-equivalent simulation above, ``_build_strategy_candidates`` builds
up to 6 more named candidate squads/captains (``app.h2h.strategy``'s own
pure re-ranking functions, plus Milestone 4's already-persisted
``TransferOptimizationRun`` scenarios for ``NO_HIT``/``CALCULATED_HIT``), and
every candidate is simulated against the SAME precomputed rival-side score
distribution (``app.h2h.simulate.sample_rival_scores``, sampled once per run)
for a fair, apples-to-apples comparison. Exactly one ``h2h_strategy_candidates``
child row is flagged ``is_recommended=True`` per run (see
``app.h2h.strategy.select_recommended_candidate`` for the tie-break rule),
and ``h2h_simulation_runs.h2h_adjustment_materially_changed`` records whether
that recommendation actually differs from EP_OPTIMAL.

Follows ``TransferOptimizationJobRunner``'s create-early-RUNNING-row
discipline: input validation, squad/profile loading, and the optimization
gate check all happen BEFORE the ``h2h_simulation_runs`` row is created (a
hard refusal on any of those never pollutes the audit trail), and the row is
created immediately after, before the actual simulation work, so a crash
mid-simulation still leaves a terminal (FAILED) row via ``_finalize_failed``.

Off-season handling deliberately differs from ``TRANSFER_OPTIMIZATION``:
that job hard-refuses when there is no unfinished gameweek to plan a
transfer horizon against (there is no meaningful *forward* transfer plan
with zero target gameweeks). A matchup SIMULATION has no such problem — it
is still meaningfully answerable off-season using each side's last-known
squad, which is exactly what a past-completed-gameweek plausibility check
needs (see ``app.h2h.simulate``'s own module docstring for why this is a
useful verification tool, not just a real-time one). So this handler never
hard-blocks on off-season: it resolves the next unfinished gameweek if one
exists, falls back to the most recently FINISHED gameweek otherwise, and
only refuses (``NO_TARGET_GAMEWEEK``) if literally no gameweek has ever
finished this season. An explicit ``parameters.target_fpl_event_id`` always
takes precedence over both.

Starting-XI resolution: neither ``ManagerSquadSnapshot.squad_player_ids`` nor
``RivalGameweekSnapshot.squad_player_ids`` preserve FPL's own pick ORDER
(both are built from a dict keyed by resolved element id, see
``app.jobs.handlers.manager_sync``/``league_sync``'s own construction) — so
which 11 of the (up to) 15 squad members are actually STARTING, and who is
captain/vice, can only be read back out of each snapshot's own
``raw_picks_json["picks"]`` (``position`` 1-11 = starting XI, ``multiplier``/
``is_captain`` for the user's deterministic captain choice).

For the USER's own snapshot, a missing ``raw_picks_json`` (every
``MANUAL_ENTRY`` squad, or a ``MANAGER_SYNC`` one whose own picks fetch
failed) falls back to ``_derive_starting_and_bench_from_solver`` -- the same
solver ``LINEUP_OPTIMIZATION`` itself uses (Milestone 8 item 5, mirroring
``CHIP_SIMULATION``'s identical fix) -- rather than hard-refusing. The RIVAL
side is never solved on the rival's behalf: their starting XI is what THEY
actually picked, so a rival snapshot with no ``raw_picks_json`` still
hard-refuses with ``RIVAL_SQUAD_UNAVAILABLE``, unchanged.

Autosub: a starting player hard-excluded from playing (``project_player_with_
champion``'s ``is_eligible=False`` — injured/suspended/unavailable) is
replaced by ``_resolve_effective_starting_xi`` with the highest-priority
ELIGIBLE bench player of the same broad class (goalkeeper-for-goalkeeper;
any outfield position for any outfield position, in the manager's own bench
order) — for BOTH the user and the rival side. This is a deliberately
simplified version of real FPL autosub: it does not enforce that the
resulting XI still satisfies each position's minimum-play count (e.g. never
fewer than 3 defenders), which would need a small formation-legality solver;
a same-class swap is correct for the overwhelming majority of real
single-injury cases but not guaranteed for unusual multi-injury/formation
combinations. On the user side, if the ORIGINAL captain is autosubbed out,
the armband transfers to the vice-captain (if they themselves are still in
the final XI) exactly once — matching FPL's own single-level captain-to-
vice-captain fallback (no further fallback if the vice is unavailable too).
An autosubbed-IN bench player can never inherit the armband either way.

Residual sourcing: ``_ResidualCache`` (Milestone 6 Layer 1: extracted
verbatim into ``app.optimization.residual_cache``, imported from there) prefers
a player's own current-season ``player_gameweek_stats`` history; when that's
empty (the realistic early-season/fresh-database state — no gameweeks of the
CURRENT season have been played/ingested yet), it falls back to a
same-position pool drawn across EVERY ingested season (matched by
``Position.short_name``, since ``Position``/``Player`` rows are
season-scoped and a position's own id differs season to season) rather than
a current-season-only pool that would otherwise be silently empty and force
every projection down to zero-variance noise until the current season's own
history accumulates.
"""

import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.h2h.simulate import (
    SIMULATION_METHOD_VERSION,
    RivalHeldPlayerInput,
    RivalTransferTargetInput,
    SimulationInputs,
    UserPlayerInput,
    sample_rival_scores,
    sample_user_scores,
    summarize_matchup,
)
from app.h2h.strategy import (
    CandidateResult,
    PickSwap,
    StarterCandidate,
    apply_pick_swaps,
    final_captain_player_id,
    h2h_adjustment_materially_changed,
    recaptain,
    select_captain_cover_captain,
    select_differential_captain,
    select_high_upside_captain,
    select_low_variance_captain,
    select_recommended_candidate,
)
from app.jobs.handlers.lineup_optimization import _build_lineup_candidates
from app.jobs.registry import register_handler
from app.jobs.runner import JobContext, JobResult, JobRunner
from app.optimization.champion_loader import LoadedChampion, load_current_champion
from app.optimization.constraints import (
    OptimizationConstraints,
    RulesBlockedError,
    load_optimization_constraints,
)
from app.optimization.gameweek_resolution import (
    resolve_most_recent_finished_gameweek_event,
    resolve_target_gameweek,
)
from app.optimization.gate import check_optimization_gate
from app.optimization.lineup_solver import solve_lineup
from app.optimization.projection import PlayerProjection, project_player_with_champion
from app.optimization.residual_cache import _ResidualCache
from packages.database.models.gameweek import Gameweek
from packages.database.models.h2h_simulation_run import H2HSimulationRun
from packages.database.models.h2h_strategy_candidate import H2HStrategyCandidate
from packages.database.models.manager_squad_snapshot import ManagerSquadSnapshot
from packages.database.models.player import Player
from packages.database.models.position import Position
from packages.database.models.rival_gameweek_snapshot import RivalGameweekSnapshot
from packages.database.models.rival_profile import RivalProfile
from packages.database.models.transfer_optimization_run import TransferOptimizationRun
from packages.database.models.transfer_optimization_run_move import TransferOptimizationRunMove
from packages.database.models.transfer_optimization_run_scenario import TransferOptimizationRunScenario
from packages.database.session import SessionLocal
from packages.domain.h2h import H2HSimulationRunStatus, H2HStrategyLabel
from packages.domain.jobs import JobStatus, JobType
from packages.domain.optimization import SolverStatus, TargetGameweekStatus
from packages.domain.transfers import TransferOptimizationRunStatus

_STARTING_XI_MAX_POSITION = 11


class _InputResolutionError(Exception):
    """Raised for a clear, user-facing input problem, always caught before any row is created."""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


class _SimulationBuildError(Exception):
    """Raised for a problem discovered AFTER the RUNNING row is created, always finalized FAILED."""


def _parse_uuid_parameter(parameters: dict | None, key: str) -> uuid.UUID:
    raw = (parameters or {}).get(key)
    if raw is None:
        raise _InputResolutionError(f"MISSING_{key.upper()}", f"parameters.{key} is required.")
    try:
        return uuid.UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        raise _InputResolutionError(
            f"INVALID_{key.upper()}", f"parameters.{key} is not a valid UUID."
        ) from None


def _split_starting_and_bench(raw_picks_json: dict | None) -> tuple[list[dict], list[dict]]:
    """Split a picks payload into (starting XI, bench-in-priority-order)."""
    if not raw_picks_json or not raw_picks_json.get("picks"):
        return [], []
    picks = raw_picks_json["picks"]
    starters = [p for p in picks if p.get("position", 99) <= _STARTING_XI_MAX_POSITION]
    bench = sorted(
        (p for p in picks if p.get("position", 0) > _STARTING_XI_MAX_POSITION),
        key=lambda p: p.get("position", 99),
    )
    return starters, bench


def _derive_starting_and_bench_from_solver(
    session: Session,
    season_id: uuid.UUID,
    squad_player_ids: list[str],
    target_gameweek_id: uuid.UUID | None,
    champion: LoadedChampion | None,
    constraints: OptimizationConstraints,
) -> tuple[list[dict], list[dict]]:
    """Fallback for the USER's own squad when it has no usable ``raw_picks_json`` (every
    MANUAL_ENTRY squad, or a MANAGER_SYNC one whose own picks fetch failed) -- mirrors
    ``app.jobs.handlers.chip_simulation``'s own private helper of the same name (duplicated, not
    imported -- private to its own module, same precedent as this module's own
    ``_split_starting_and_bench`` already being duplicated there). NEVER used for the rival's
    side: a rival's starting XI is what THEY actually picked, not something to optimize on their
    behalf, so ``rival_snapshot``/``rival_gameweek_snapshots`` without ``raw_picks_json`` still
    hard-refuse with ``RIVAL_SQUAD_UNAVAILABLE`` above, unchanged.

    A manually-entered squad was never assigned a real starting XI/bench/captain by an actual
    human, so there is nothing in ``raw_picks_json`` to reuse verbatim -- the honest substitute is
    the SAME solver ``LINEUP_OPTIMIZATION`` itself uses (``app.optimization.lineup_solver.
    solve_lineup``, via the same ``app.jobs.handlers.lineup_optimization._build_lineup_candidates``
    candidate-building reused directly rather than duplicated), reshaped into the identical
    picks-dict shape ``_split_starting_and_bench`` produces so every downstream consumer here
    (autosub, captain resolution, EP_OPTIMAL and every strategy candidate) is unchanged.
    """
    player_ids = [uuid.UUID(pid) for pid in squad_player_ids]
    candidates = _build_lineup_candidates(session, season_id, player_ids, target_gameweek_id, champion)
    solution = solve_lineup(candidates, constraints.position_requirements)
    if solution.solver_status != SolverStatus.OPTIMAL:
        return [], []

    players_by_id = {
        p.id: p for p in session.execute(select(Player).where(Player.id.in_(player_ids))).scalars()
    }

    starters = [
        {
            "element": players_by_id[player_id].fpl_element_id,
            "position": index + 1,
            "is_captain": player_id == solution.captain_player_id,
        }
        for index, player_id in enumerate(solution.starting_player_ids)
        if player_id in players_by_id
    ]
    bench = [
        {
            "element": players_by_id[player_id].fpl_element_id,
            "position": _STARTING_XI_MAX_POSITION + 1 + index,
            "is_captain": False,
        }
        for index, player_id in enumerate(solution.bench_player_ids_ordered)
        if player_id in players_by_id
    ]
    return starters, bench


def _resolve_players_by_element(
    session: Session, season_id: uuid.UUID, element_ids: set[int]
) -> dict[int, Player]:
    if not element_ids:
        return {}
    players = (
        session.execute(
            select(Player).where(Player.season_id == season_id, Player.fpl_element_id.in_(element_ids))
        )
        .scalars()
        .all()
    )
    return {p.fpl_element_id: p for p in players}


def _positions_by_id(session: Session, season_id: uuid.UUID) -> dict[uuid.UUID, str]:
    rows = session.execute(select(Position).where(Position.season_id == season_id)).scalars().all()
    return {p.id: p.short_name for p in rows}


def _position_short_name(positions_by_id: dict[uuid.UUID, str], player: Player) -> str | None:
    if player.position_id is None:
        return None
    return positions_by_id.get(player.position_id)


@dataclass(frozen=True)
class _ResolvedSlot:
    """One final starting-XI slot for a side, after a simplified real-FPL autosub.

    ``original_starter_element`` is always the ORIGINAL starting-XI pick's
    raw FPL element id this slot corresponds to -- used only to determine
    whether the user's captain/vice-captain armband survives (an autosubbed-
    IN bench player can never inherit it, see ``_resolve_captain_multipliers``).
    ``player``/``projection`` are for whichever player will actually be
    scored in this slot: the original starter, or their autosub replacement.
    """

    original_starter_element: int
    is_autosub: bool
    player: Player
    projection: PlayerProjection


def _resolve_effective_starting_xi(
    session: Session,
    starters: list[dict],
    bench: list[dict],
    player_by_element: dict[int, Player],
    positions_by_id: dict[uuid.UUID, str],
    target_gameweek_id: uuid.UUID,
    champion: LoadedChampion | None,
) -> list[_ResolvedSlot]:
    """Resolve the EFFECTIVE starting XI: apply a simplified real-FPL autosub. See module docstring."""

    def _resolve(pick: dict) -> tuple[dict, Player, PlayerProjection] | None:
        player = player_by_element.get(pick["element"])
        if player is None:
            return None  # unresolved element (renamed/removed player) -- degrades gracefully
        projection = project_player_with_champion(session, player, target_gameweek_id, champion)
        return pick, player, projection

    resolved_starters = [r for r in (_resolve(p) for p in starters) if r is not None]
    resolved_bench = [r for r in (_resolve(p) for p in bench) if r is not None]

    def _is_gk(player: Player) -> bool:
        return _position_short_name(positions_by_id, player) == "GKP"

    gk_bench = [r for r in resolved_bench if _is_gk(r[1])]
    outfield_bench = [r for r in resolved_bench if not _is_gk(r[1])]
    used_bench_elements: set[int] = set()

    slots: list[_ResolvedSlot] = []
    for pick, player, projection in resolved_starters:
        if projection.is_eligible:
            slots.append(_ResolvedSlot(pick["element"], False, player, projection))
            continue
        queue = gk_bench if _is_gk(player) else outfield_bench
        replacement = next(
            (r for r in queue if r[0]["element"] not in used_bench_elements and r[2].is_eligible), None
        )
        if replacement is None:
            slots.append(_ResolvedSlot(pick["element"], False, player, projection))  # no eligible sub
            continue
        used_bench_elements.add(replacement[0]["element"])
        slots.append(_ResolvedSlot(pick["element"], True, replacement[1], replacement[2]))
    return slots


def _resolve_captain_multipliers(starters: list[dict], replaced_elements: set[int]) -> dict[int, int]:
    """Map ORIGINAL starter element id -> final multiplier, transferring the armband captain-to-vice
    exactly once when the captain was autosubbed out (real FPL has no further fallback). Absent keys
    default to multiplier 1 for the caller.
    """
    captain_pick = next((p for p in starters if p.get("is_captain")), None)
    if captain_pick is None:
        return {}
    captain_multiplier_value = captain_pick.get("multiplier", 1)
    captain_element = captain_pick["element"]
    if captain_element not in replaced_elements:
        return {captain_element: captain_multiplier_value}
    vice_pick = next((p for p in starters if p.get("is_vice_captain")), None)
    if vice_pick is not None and vice_pick["element"] not in replaced_elements:
        return {vice_pick["element"]: captain_multiplier_value}
    return {}  # captain AND vice both unavailable this gameweek -- no armband boost, matches real FPL


def _build_user_players_from_picks(
    session: Session,
    season_id: uuid.UUID,
    starters: list[dict],
    bench: list[dict],
    target_gameweek_id: uuid.UUID,
    champion: LoadedChampion | None,
    residuals: _ResidualCache,
) -> tuple[list[UserPlayerInput], list[_ResolvedSlot]]:
    """Resolve one (starters, bench) picks pair into (user_players, resolved_slots).

    Also returns the resolved slots (not just the ``UserPlayerInput`` list) so Layer 4's
    strategy-candidate generation can read each starter's ``Player`` row directly (appearance-risk,
    ownership) without a second DB round trip — see
    ``app.jobs.handlers.h2h_simulation.H2HSimulationJobRunner`` for how this is used.
    """
    if not starters:
        raise _SimulationBuildError("User picks' starting XI could not be read from raw_picks_json.")

    player_by_element = _resolve_players_by_element(
        session, season_id, {p["element"] for p in starters + bench}
    )
    positions_by_id = _positions_by_id(session, season_id)

    slots = _resolve_effective_starting_xi(
        session, starters, bench, player_by_element, positions_by_id, target_gameweek_id, champion
    )
    replaced_elements = {s.original_starter_element for s in slots if s.is_autosub}
    multiplier_by_original_element = _resolve_captain_multipliers(starters, replaced_elements)

    user_players: list[UserPlayerInput] = []
    for slot in slots:
        multiplier = (
            1 if slot.is_autosub else multiplier_by_original_element.get(slot.original_starter_element, 1)
        )
        position_short_name = _position_short_name(positions_by_id, slot.player)
        user_players.append(
            UserPlayerInput(
                player_id=str(slot.player.id),
                projected_points=slot.projection.projected_points,
                multiplier=multiplier,
                own_residuals=residuals.for_player(slot.player.id),
                position_residuals=residuals.for_position(slot.player.position_id, position_short_name),
            )
        )
    if not user_players:
        raise _SimulationBuildError(
            "None of the user's starting XI elements resolved to current Player rows."
        )
    return user_players, slots


def _build_rival_held_players(
    session: Session,
    season_id: uuid.UUID,
    rival_snapshot: RivalGameweekSnapshot,
    target_gameweek_id: uuid.UUID,
    champion: LoadedChampion | None,
    residuals: _ResidualCache,
) -> list[RivalHeldPlayerInput]:
    starters, bench = _split_starting_and_bench(rival_snapshot.raw_picks_json)
    if not starters:
        raise _SimulationBuildError("Rival snapshot's starting XI could not be read from raw_picks_json.")

    player_by_element = _resolve_players_by_element(
        session, season_id, {p["element"] for p in starters + bench}
    )
    positions_by_id = _positions_by_id(session, season_id)

    slots = _resolve_effective_starting_xi(
        session, starters, bench, player_by_element, positions_by_id, target_gameweek_id, champion
    )

    held: list[RivalHeldPlayerInput] = []
    for slot in slots:
        position_short_name = _position_short_name(positions_by_id, slot.player)
        held.append(
            RivalHeldPlayerInput(
                player_id=str(slot.player.id),
                fpl_element_id=slot.player.fpl_element_id,
                position=position_short_name,
                projected_points=slot.projection.projected_points,
                own_residuals=residuals.for_player(slot.player.id),
                position_residuals=residuals.for_position(slot.player.position_id, position_short_name),
            )
        )
    if not held:
        raise _SimulationBuildError(
            "None of the rival's starting XI elements resolved to current Player rows."
        )
    return held


def _build_rival_transfer_targets(
    session: Session,
    season_id: uuid.UUID,
    candidates_json: list | None,
    target_gameweek_id: uuid.UUID,
    champion: LoadedChampion | None,
    residuals: _ResidualCache,
) -> list[RivalTransferTargetInput]:
    if not candidates_json:
        return []

    targets: list[RivalTransferTargetInput] = []
    for candidate in candidates_json:
        try:
            player_id = uuid.UUID(str(candidate["player_id"]))
        except (KeyError, ValueError, TypeError):
            continue
        player = session.get(Player, player_id)
        if player is None or player.season_id != season_id:
            continue  # stale/cross-season candidate -- skip rather than fail the whole run
        projection = project_player_with_champion(session, player, target_gameweek_id, champion)
        targets.append(
            RivalTransferTargetInput(
                player_id=str(player.id),
                position=candidate.get("position"),
                projected_points=projection.projected_points,
                own_residuals=residuals.for_player(player.id),
                position_residuals=residuals.for_position(player.position_id, candidate.get("position")),
            )
        )
    return targets


def _starter_candidates(
    slots: list[_ResolvedSlot], user_players: list[UserPlayerInput]
) -> list[StarterCandidate]:
    """Zip EP_OPTIMAL's own resolved slots with their already-built ``UserPlayerInput`` twins into
    the richer ``StarterCandidate`` shape Layer 4's captain-re-ranking functions need (ownership,
    appearance-risk) -- zero extra DB queries, both lists were already built 1:1 by
    ``_build_user_players_from_picks``.
    """
    return [
        StarterCandidate(
            player_id=slot.player.id,
            projected_points=up.projected_points,
            own_residuals=up.own_residuals,
            position_residuals=up.position_residuals,
            chance_of_playing_next_round=slot.player.chance_of_playing_next_round,
            selected_by_percent=(
                float(slot.player.selected_by_percent)
                if slot.player.selected_by_percent is not None
                else None
            ),
        )
        for slot, up in zip(slots, user_players, strict=True)
    ]


def _latest_succeeded_transfer_optimization_run(
    session: Session, manager_squad_snapshot_id: uuid.UUID
) -> TransferOptimizationRun | None:
    """Most recent SUCCEEDED ``TransferOptimizationRun`` for this exact snapshot, if any.

    H2H_SIMULATION only ever READS an already-persisted Milestone 4 result
    here -- it never triggers a fresh TRANSFER_OPTIMIZATION run itself as a
    side effect (that remains a distinct, separately-triggered job).
    """
    return session.execute(
        select(TransferOptimizationRun)
        .where(
            TransferOptimizationRun.manager_squad_snapshot_id == manager_squad_snapshot_id,
            TransferOptimizationRun.run_status == TransferOptimizationRunStatus.SUCCEEDED,
        )
        .order_by(TransferOptimizationRun.completed_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _scenario_by_transfers_count(
    session: Session, transfer_optimization_run_id: uuid.UUID, transfers_count: int
) -> TransferOptimizationRunScenario | None:
    return session.execute(
        select(TransferOptimizationRunScenario).where(
            TransferOptimizationRunScenario.transfer_optimization_run_id == transfer_optimization_run_id,
            TransferOptimizationRunScenario.transfers_count == transfers_count,
        )
    ).scalar_one_or_none()


def _pick_swaps_for_scenario(session: Session, scenario_id: uuid.UUID) -> list[PickSwap]:
    """Resolve one scenario's persisted moves into element-id swaps for ``app.h2h.strategy.apply_pick_swaps``.

    A move whose ``player_out_id``/``player_in_id`` no longer resolves to a
    ``Player`` row is skipped (degrades gracefully) rather than failing the
    whole candidate -- should not happen for a freshly-persisted Milestone 4
    move, but this module never fabricates a swap it can't fully resolve.
    """
    moves = (
        session.execute(
            select(TransferOptimizationRunMove).where(
                TransferOptimizationRunMove.transfer_optimization_run_scenario_id == scenario_id
            )
        )
        .scalars()
        .all()
    )
    swaps: list[PickSwap] = []
    for move in moves:
        player_out = session.get(Player, move.player_out_id)
        player_in = session.get(Player, move.player_in_id)
        if player_out is None or player_in is None:
            continue
        swaps.append(
            PickSwap(
                player_out_element_id=player_out.fpl_element_id,
                player_in_element_id=player_in.fpl_element_id,
            )
        )
    return swaps


@dataclass(frozen=True)
class _BuiltCandidate:
    """One strategy candidate's final (label, user-side squad, provenance scenario) -- BEFORE any
    simulation happens. Simulating each against the one shared, precomputed rival distribution is
    the caller's (``H2HSimulationJobRunner.run``'s) job, so every candidate is compared fairly.
    """

    strategy_label: H2HStrategyLabel
    user_players: tuple[UserPlayerInput, ...]
    transfer_optimization_run_scenario_id: uuid.UUID | None


def _build_strategy_candidates(
    session: Session,
    season_id: uuid.UUID,
    manager_squad_snapshot_id: uuid.UUID,
    ep_optimal_starters: list[dict],
    ep_optimal_bench: list[dict],
    ep_optimal_user_players: list[UserPlayerInput],
    ep_optimal_slots: list[_ResolvedSlot],
    target_gameweek_id: uuid.UUID,
    champion: LoadedChampion | None,
    residuals: _ResidualCache,
) -> list[_BuiltCandidate]:
    """Build every one of the (up to) 7 named candidates' squads, reusing EP_OPTIMAL's own already-
    resolved starting XI wherever a candidate doesn't change squad membership (LOW_VARIANCE/
    HIGH_UPSIDE/CAPTAIN_COVER/DIFFERENTIAL_CAPTAIN only ever re-pick a captain, never a player;
    NO_HIT reuses the squad verbatim, unchanged) and Milestone 4's own persisted scenario search for
    the one candidate that does change squad membership (CALCULATED_HIT). NO_HIT/CALCULATED_HIT are
    both skipped together when no prior ``TransferOptimizationRun`` exists for this snapshot -- see
    this module's own docstring and ``app.h2h.strategy``'s for the exact judgment calls behind each
    candidate's construction.
    """
    built = [_BuiltCandidate(H2HStrategyLabel.EP_OPTIMAL, tuple(ep_optimal_user_players), None)]

    starter_candidates = _starter_candidates(ep_optimal_slots, ep_optimal_user_players)
    for label, select_captain in (
        (H2HStrategyLabel.LOW_VARIANCE, select_low_variance_captain),
        (H2HStrategyLabel.HIGH_UPSIDE, select_high_upside_captain),
        (H2HStrategyLabel.CAPTAIN_COVER, select_captain_cover_captain),
        (H2HStrategyLabel.DIFFERENTIAL_CAPTAIN, select_differential_captain),
    ):
        new_captain_player_id = str(select_captain(starter_candidates))
        recaptained = recaptain(tuple(ep_optimal_user_players), new_captain_player_id)
        built.append(_BuiltCandidate(label, recaptained, None))

    transfer_run = _latest_succeeded_transfer_optimization_run(session, manager_squad_snapshot_id)

    # NO_HIT: only generated when a real TransferOptimizationRun exists to cite as
    # provenance for its 0-transfer (roll) scenario -- skipped alongside CALCULATED_HIT
    # when none exists, matching this task's own validation criteria (5 candidates, both
    # transfer-derived strategies absent, when no prior run exists). Its SQUAD is always
    # EP_OPTIMAL's own verbatim regardless (a 0-transfer scenario is definitionally "no
    # squad change") -- what differs is only whether a real scenario row backs it.
    if transfer_run is not None:
        no_hit_scenario = _scenario_by_transfers_count(session, transfer_run.id, 0)
        if no_hit_scenario is not None:
            built.append(
                _BuiltCandidate(H2HStrategyLabel.NO_HIT, tuple(ep_optimal_user_players), no_hit_scenario.id)
            )

    # CALCULATED_HIT: only generated when a prior run exists AND its own recommended
    # scenario actually clears a hit cost (transfers_count 1 or 2) -- never fabricated
    # when the best real Milestone 4 recommendation was itself to roll.
    if transfer_run is not None and transfer_run.recommended_transfers_count in (1, 2):
        calculated_hit_scenario = _scenario_by_transfers_count(
            session, transfer_run.id, transfer_run.recommended_transfers_count
        )
        if calculated_hit_scenario is not None:
            swaps = _pick_swaps_for_scenario(session, calculated_hit_scenario.id)
            if swaps:
                combined_picks = apply_pick_swaps(ep_optimal_starters + ep_optimal_bench, swaps)
                new_starters, new_bench = _split_starting_and_bench({"picks": combined_picks})
                calculated_hit_user_players, _ = _build_user_players_from_picks(
                    session, season_id, new_starters, new_bench, target_gameweek_id, champion, residuals
                )
                built.append(
                    _BuiltCandidate(
                        H2HStrategyLabel.CALCULATED_HIT,
                        tuple(calculated_hit_user_players),
                        calculated_hit_scenario.id,
                    )
                )

    return built


def _candidate_rationale(
    candidate: CandidateResult, recommended: CandidateResult, ep_optimal_win_probability: float
) -> str:
    if candidate is recommended:
        if candidate.strategy_label == H2HStrategyLabel.EP_OPTIMAL:
            return (
                "Recommended: no alternative candidate produced a strictly higher win probability, "
                "and EP_OPTIMAL is preferred whenever the comparison ties."
            )
        return (
            f"Recommended: {candidate.win_probability:.1%} win probability, the highest among all "
            "evaluated candidates for this matchup."
        )
    delta = candidate.win_probability - ep_optimal_win_probability
    return (
        f"Not recommended: {candidate.win_probability:.1%} win probability vs EP_OPTIMAL's "
        f"{ep_optimal_win_probability:.1%} ({delta:+.1%})."
    )


def _resolve_target_event(
    session: Session, season_id: uuid.UUID, explicit_event_id: int | None
) -> tuple[uuid.UUID, int]:
    """Resolve (internal gameweek id, raw fpl event id) for the gameweek to simulate.

    An explicit ``parameters.target_fpl_event_id`` always wins (this is what
    a past-completed-gameweek plausibility check supplies). Otherwise prefers
    the next unfinished gameweek; falls back to the most recently FINISHED
    one off-season (see module docstring for why this is a deliberate,
    documented departure from TRANSFER_OPTIMIZATION's hard off-season
    refusal). Raises ``_InputResolutionError`` if neither can be resolved.
    """
    if explicit_event_id is not None:
        gameweek_id = session.execute(
            select(Gameweek.id).where(
                Gameweek.season_id == season_id, Gameweek.fpl_event_id == explicit_event_id
            )
        ).scalar_one_or_none()
        if gameweek_id is None:
            raise _InputResolutionError(
                "GAMEWEEK_NOT_FOUND", f"No gameweek found for fpl_event_id={explicit_event_id}."
            )
        return gameweek_id, explicit_event_id

    gameweek_id, status = resolve_target_gameweek(session, season_id)
    if status == TargetGameweekStatus.RESOLVED:
        assert gameweek_id is not None
        gameweek = session.get(Gameweek, gameweek_id)
        assert gameweek is not None
        return gameweek_id, gameweek.fpl_event_id

    fallback_event_id = resolve_most_recent_finished_gameweek_event(session, season_id)
    if fallback_event_id is None:
        raise _InputResolutionError(
            "NO_TARGET_GAMEWEEK", "No unfinished or finished gameweek exists yet for this season."
        )
    fallback_gameweek_id = session.execute(
        select(Gameweek.id).where(Gameweek.season_id == season_id, Gameweek.fpl_event_id == fallback_event_id)
    ).scalar_one()
    return fallback_gameweek_id, fallback_event_id


@register_handler(JobType.H2H_SIMULATION)
class H2HSimulationJobRunner(JobRunner):
    """Monte Carlo-simulates one user-vs-rival H2H matchup for a target gameweek."""

    def run(self, ctx: JobContext) -> JobResult:
        session = SessionLocal()
        run_row: H2HSimulationRun | None = None
        try:
            try:
                manager_squad_snapshot_id = _parse_uuid_parameter(ctx.parameters, "manager_squad_snapshot_id")
                rival_profile_id = _parse_uuid_parameter(ctx.parameters, "rival_profile_id")

                snapshot = session.get(ManagerSquadSnapshot, manager_squad_snapshot_id)
                if snapshot is None:
                    raise _InputResolutionError(
                        "SNAPSHOT_NOT_FOUND",
                        f"manager_squad_snapshots row {manager_squad_snapshot_id} not found.",
                    )
                rival_profile = session.get(RivalProfile, rival_profile_id)
                if rival_profile is None:
                    raise _InputResolutionError(
                        "RIVAL_PROFILE_NOT_FOUND", f"rival_profiles row {rival_profile_id} not found."
                    )
                if snapshot.season_id != rival_profile.season_id:
                    raise _InputResolutionError(
                        "SEASON_MISMATCH",
                        "manager_squad_snapshot and rival_profile belong to different seasons.",
                    )
                season_id = snapshot.season_id

                # No upfront raw_picks_json gate for the USER's own snapshot: one without it
                # (every MANUAL_ENTRY squad, or a MANAGER_SYNC one whose own picks fetch failed)
                # falls back to solving a starting XI instead, once execution reaches that point
                # below -- see _derive_starting_and_bench_from_solver's own docstring. The RIVAL
                # side has no such fallback and keeps its own hard gate right below: their
                # starting XI is what THEY actually picked, not something to optimize for them.

                if rival_profile.last_known_snapshot_id is None:
                    raise _InputResolutionError(
                        "RIVAL_SQUAD_UNAVAILABLE", "rival_profile has no last-known squad snapshot."
                    )
                rival_snapshot = session.get(RivalGameweekSnapshot, rival_profile.last_known_snapshot_id)
                if (
                    rival_snapshot is None
                    or not rival_snapshot.raw_picks_json
                    or not rival_snapshot.raw_picks_json.get("picks")
                ):
                    raise _InputResolutionError(
                        "RIVAL_SQUAD_UNAVAILABLE",
                        "rival_profile's last-known snapshot has no raw_picks_json to resolve a starting "
                        "XI from.",
                    )

                gate = check_optimization_gate(session, season_id)
                if not gate.allowed:
                    raise _InputResolutionError(
                        gate.reason_code or "GATE_REFUSED", "Optimization gate refused."
                    )

                try:
                    constraints = load_optimization_constraints(session, season_id)
                except RulesBlockedError as exc:
                    raise _InputResolutionError("RULES_BLOCKED", str(exc)) from exc

                raw_target_event = (ctx.parameters or {}).get("target_fpl_event_id")
                explicit_event_id = int(raw_target_event) if raw_target_event is not None else None
                target_gameweek_id, target_fpl_event_id = _resolve_target_event(
                    session, season_id, explicit_event_id
                )
            except _InputResolutionError as exc:
                return JobResult(
                    status=JobStatus.FAILED, output=None, error_code=exc.error_code, error_message=str(exc)
                )

            try:
                raw_match_id = (ctx.parameters or {}).get("h2h_match_fpl_id")
                started_at = datetime.now(UTC)
                run_row = H2HSimulationRun(
                    job_id=uuid.UUID(ctx.job_id),
                    manager_squad_snapshot_id=snapshot.id,
                    rival_profile_id=rival_profile.id,
                    h2h_match_fpl_id=int(raw_match_id) if raw_match_id is not None else None,
                    run_status=H2HSimulationRunStatus.RUNNING,
                    target_fpl_event_id=target_fpl_event_id,
                    simulation_method_version=SIMULATION_METHOD_VERSION,
                    rules_status_snapshot=gate.rules_status,
                    optimization_permitted_snapshot=gate.optimization_permitted,
                    data_quality_score_snapshot=(
                        Decimal(str(gate.data_quality_score)) if gate.data_quality_score is not None else None
                    ),
                    parameters_json=ctx.parameters,
                    started_at=started_at,
                )
                session.add(run_row)
                session.commit()
                session.refresh(run_row)

                champion = load_current_champion(session)
                residuals = _ResidualCache(session, season_id)

                if snapshot.raw_picks_json and snapshot.raw_picks_json.get("picks"):
                    ep_optimal_starters, ep_optimal_bench = _split_starting_and_bench(snapshot.raw_picks_json)
                else:
                    ep_optimal_starters, ep_optimal_bench = _derive_starting_and_bench_from_solver(
                        session,
                        season_id,
                        snapshot.squad_player_ids,
                        target_gameweek_id,
                        champion,
                        constraints,
                    )
                if not ep_optimal_starters:
                    raise _SimulationBuildError(
                        "Could not resolve a starting XI for the user's own squad, neither from its "
                        "own raw_picks_json nor by solving one -- see "
                        "_derive_starting_and_bench_from_solver's own docstring for when this "
                        "fallback itself can still come back empty (solver infeasibility)."
                    )
                ep_optimal_user_players, ep_optimal_slots = _build_user_players_from_picks(
                    session,
                    season_id,
                    ep_optimal_starters,
                    ep_optimal_bench,
                    target_gameweek_id,
                    champion,
                    residuals,
                )
                rival_held_players = _build_rival_held_players(
                    session, season_id, rival_snapshot, target_gameweek_id, champion, residuals
                )
                rival_transfer_targets = _build_rival_transfer_targets(
                    session,
                    season_id,
                    rival_profile.predicted_likely_transfer_targets_json,
                    target_gameweek_id,
                    champion,
                    residuals,
                )

                base_inputs = SimulationInputs(
                    user_players=tuple(ep_optimal_user_players),
                    rival_held_players=tuple(rival_held_players),
                    rival_transfer_targets=tuple(rival_transfer_targets),
                    rival_no_transfer_probability=float(rival_profile.predicted_no_transfer_probability or 0),
                    rival_one_transfer_probability=float(
                        rival_profile.predicted_one_transfer_probability or 0
                    ),
                    rival_multi_transfer_probability=float(
                        rival_profile.predicted_multi_transfer_probability or 0
                    ),
                    rival_captain_probabilities=dict(
                        rival_profile.predicted_captain_probabilities_json or {}
                    ),
                    rival_state_confidence=rival_profile.overall_confidence,
                )

                # Milestone 5 Layer 4: every one of the (up to) 7 named strategy candidates is
                # simulated against this SAME precomputed rival distribution -- sampled exactly
                # once per run, never re-randomized per candidate -- so every candidate's
                # win_probability differs ONLY because of its own user-side squad/captain, never
                # because it happened to draw a luckier/unluckier rival sample. See
                # app.h2h.simulate.sample_rival_scores's own docstring.
                rng = random.Random()
                rival_scores = sample_rival_scores(base_inputs, rng)

                built_candidates = _build_strategy_candidates(
                    session,
                    season_id,
                    snapshot.id,
                    ep_optimal_starters,
                    ep_optimal_bench,
                    ep_optimal_user_players,
                    ep_optimal_slots,
                    target_gameweek_id,
                    champion,
                    residuals,
                )

                ep_optimal_result = None
                ep_optimal_final_captain: str | None = None
                candidate_results: list[CandidateResult] = []
                # Two candidates whose user_players are LITERALLY identical (same squad AND same
                # captain -- e.g. NO_HIT vs EP_OPTIMAL, or a captain-strategy that happens to
                # re-pick the already-current captain) represent the exact same real-world decision
                # and must therefore tie EXACTLY, not merely "probably tie" up to sampling noise --
                # re-sampling them independently could otherwise let pure Monte Carlo noise flip
                # is_recommended/h2h_adjustment_materially_changed between two candidates that are,
                # in reality, the identical choice. Cached by value (UserPlayerInput is a frozen,
                # hashable dataclass) so this happens automatically, not via special-casing labels.
                user_scores_by_squad: dict[tuple[UserPlayerInput, ...], tuple[int, ...]] = {}
                for built in built_candidates:
                    if built.user_players not in user_scores_by_squad:
                        user_scores_by_squad[built.user_players] = sample_user_scores(
                            built.user_players, rng, base_inputs.sample_count
                        )
                    user_scores = user_scores_by_squad[built.user_players]
                    summary = summarize_matchup(user_scores, rival_scores, rival_profile.overall_confidence)
                    this_final_captain = final_captain_player_id(built.user_players)

                    if built.strategy_label == H2HStrategyLabel.EP_OPTIMAL:
                        ep_optimal_result = summary
                        ep_optimal_final_captain = this_final_captain
                        candidate_captain_player_id = None
                    else:
                        candidate_captain_player_id = (
                            uuid.UUID(this_final_captain)
                            if this_final_captain is not None
                            and this_final_captain != ep_optimal_final_captain
                            else None
                        )

                    candidate_results.append(
                        CandidateResult(
                            strategy_label=built.strategy_label,
                            win_probability=summary.win_probability,
                            draw_probability=summary.draw_probability,
                            loss_probability=summary.loss_probability,
                            expected_margin=summary.expected_margin,
                            transfer_optimization_run_scenario_id=built.transfer_optimization_run_scenario_id,
                            captain_player_id=candidate_captain_player_id,
                        )
                    )
                assert ep_optimal_result is not None  # EP_OPTIMAL is always the first built candidate

                # run_row's own top-level fields stay exactly the EP_OPTIMAL-equivalent result --
                # unchanged Layer 3 contract every existing caller/test already depends on.
                run_row.sample_count = ep_optimal_result.sample_count
                run_row.user_score_mean = Decimal(str(round(ep_optimal_result.user_score_mean, 2)))
                run_row.user_score_floor = Decimal(str(round(ep_optimal_result.user_score_floor, 2)))
                run_row.user_score_ceiling = Decimal(str(round(ep_optimal_result.user_score_ceiling, 2)))
                run_row.rival_score_mean = Decimal(str(round(ep_optimal_result.rival_score_mean, 2)))
                run_row.rival_score_floor = Decimal(str(round(ep_optimal_result.rival_score_floor, 2)))
                run_row.rival_score_ceiling = Decimal(str(round(ep_optimal_result.rival_score_ceiling, 2)))
                run_row.win_probability = Decimal(str(round(ep_optimal_result.win_probability, 4)))
                run_row.draw_probability = Decimal(str(round(ep_optimal_result.draw_probability, 4)))
                run_row.loss_probability = Decimal(str(round(ep_optimal_result.loss_probability, 4)))
                run_row.expected_margin = Decimal(str(round(ep_optimal_result.expected_margin, 2)))
                run_row.margin_std_dev = Decimal(str(round(ep_optimal_result.margin_std_dev, 2)))
                run_row.confidence = ep_optimal_result.confidence

                recommended = select_recommended_candidate(candidate_results)
                candidate_row_by_label: dict[H2HStrategyLabel, H2HStrategyCandidate] = {}
                for candidate in candidate_results:
                    row = H2HStrategyCandidate(
                        h2h_simulation_run_id=run_row.id,
                        strategy_label=candidate.strategy_label,
                        transfer_optimization_run_scenario_id=candidate.transfer_optimization_run_scenario_id,
                        captain_player_id=candidate.captain_player_id,
                        win_probability=Decimal(str(round(candidate.win_probability, 4))),
                        draw_probability=Decimal(str(round(candidate.draw_probability, 4))),
                        loss_probability=Decimal(str(round(candidate.loss_probability, 4))),
                        expected_margin=Decimal(str(round(candidate.expected_margin, 2))),
                        is_recommended=candidate is recommended,
                        rationale=_candidate_rationale(
                            candidate, recommended, ep_optimal_result.win_probability
                        ),
                    )
                    session.add(row)
                    candidate_row_by_label[candidate.strategy_label] = row
                session.flush()  # populate each row.id for recommended_candidate_id below

                run_row.recommended_candidate_id = candidate_row_by_label[recommended.strategy_label].id
                run_row.h2h_adjustment_materially_changed = h2h_adjustment_materially_changed(recommended)
                run_row.run_status = H2HSimulationRunStatus.SUCCEEDED
                run_row.completed_at = datetime.now(UTC)
                session.commit()

                return JobResult(
                    status=JobStatus.SUCCEEDED,
                    output={
                        "h2h_simulation_run_id": str(run_row.id),
                        "win_probability": float(ep_optimal_result.win_probability),
                        "draw_probability": float(ep_optimal_result.draw_probability),
                        "loss_probability": float(ep_optimal_result.loss_probability),
                        "expected_margin": float(ep_optimal_result.expected_margin),
                        "confidence": ep_optimal_result.confidence.value,
                        "recommended_strategy": recommended.strategy_label.value,
                        "h2h_adjustment_materially_changed": run_row.h2h_adjustment_materially_changed,
                    },
                    error_code=None,
                    error_message=None,
                )
            except Exception as exc:  # noqa: BLE001 - the run row must always reach a terminal status
                session.rollback()
                self._finalize_failed(session, run_row)
                return JobResult(
                    status=JobStatus.FAILED,
                    output={"h2h_simulation_run_id": str(run_row.id)} if run_row is not None else None,
                    error_code="H2H_SIMULATION_FAILED",
                    error_message=str(exc),
                )
        finally:
            session.close()

    def _finalize_failed(self, session: Session, run_row: H2HSimulationRun | None) -> None:
        if run_row is None or run_row.id is None:
            return
        try:
            fresh = session.get(H2HSimulationRun, run_row.id)
            if fresh is not None and fresh.run_status == H2HSimulationRunStatus.RUNNING:
                fresh.run_status = H2HSimulationRunStatus.FAILED
                fresh.completed_at = datetime.now(UTC)
                session.commit()
        except Exception:  # noqa: BLE001 - best-effort finalization only
            session.rollback()
