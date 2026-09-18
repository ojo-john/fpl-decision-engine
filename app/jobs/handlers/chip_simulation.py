"""CHIP_SIMULATION job: evaluate all 4 chips' currently-open window and recommend USE_NOW/HOLD/etc.

Milestone 6 Layer 3 — wires Layer 1 (``app.chips.windows``/``app.chips.inventory``:
chip scheduling windows and the user's own play-history classification) and
Layer 2 (``app.chips.scenario``: per-chip gain evaluation and USE_NOW/HOLD/
NO_VIABLE_WINDOW ranking) into a persisted job handler, mirroring
``H2HSimulationJobRunner``/``TransferOptimizationJobRunner``'s exact
create-early-RUNNING-row discipline: input validation, snapshot/rules-config
loading, and the optimization gate check all happen BEFORE the
``chip_optimization_runs`` row is created (a hard refusal on any of those
never pollutes the audit trail), and the row is created immediately after,
before the actual per-chip evaluation work, so a crash mid-run still leaves a
terminal (FAILED) row via ``_finalize_failed``.

Off-season handling deliberately mirrors ``TRANSFER_OPTIMIZATION`` (hard
refusal, no row created), NOT ``H2H_SIMULATION``'s softer "fall back to the
most recently finished gameweek" choice: chip timing is inherently
forward-looking (there is no meaningful "should I use my Wildcard" question
about a gameweek that has already been played), unlike a past-completed-
gameweek matchup plausibility check, which remains a genuinely useful
verification tool off-season. So ``NO_ACTIVE_GAMEWEEK`` here always means "no
row created", never a fallback target gameweek.

One chip type, one recommendation: for each requested ``ChipType``, this
handler finds the ONE scheduling window (of that chip's typically two) whose
``[start_event, stop_event]`` actually contains the resolved target gameweek
(real FPL's own two windows per chip are contiguous halves of the season, so
at most one window can ever contain a given gameweek) and evaluates ONLY that
window — a window not yet open, or already closed, is never evaluated (there
is nothing yet, or nothing left, to recommend there). Verdict mapping for a
chip whose current window turns out NOT to be genuinely ``AVAILABLE``:

- ``UserChipWindowStatus.USED`` -> ``ChipVerdict.ALREADY_USED`` (a real,
  already-played use exists in this exact window).
- ``UserChipWindowStatus.ACTIVE_THIS_GAMEWEEK`` -> ``ChipVerdict.UNAVAILABLE``
  (the chip is literally being played THIS gameweek — no longer a "should I
  use it" decision, it has already been made).
- ``UserChipWindowStatus.EXPIRED_UNUSED`` -> ``ChipVerdict.EXPIRED_UNUSED``
  directly (should not actually occur for a window that CONTAINS the target
  gameweek, per ``derive_user_chip_windows``'s own precedence rules, but
  mapped defensively rather than assumed impossible).
- No window at all contains the target gameweek (a genuine scheduling gap) ->
  ``ChipVerdict.NO_VIABLE_WINDOW``.

None of these produce ``chip_window_candidates`` rows — only a
``chip_recommendations`` row with ``recommended_candidate_id=None``.

Starting-XI/bench/captain resolution: unlike ``H2H_SIMULATION``, this handler
does NOT run ``_resolve_effective_starting_xi``'s real-FPL autosub logic
(that function is private to ``app.jobs.handlers.h2h_simulation`` and tightly
coupled to that module's own construction) — every one of the 4 chip
evaluators here reads the CURRENT starting XI/bench/captain straight off
``snapshot.raw_picks_json`` and reuses it, verbatim, across every candidate
gameweek being evaluated (even ones several weeks in the future). This is a
deliberate, documented simplification in the same spirit as Layer 2's own
"Wildcard/Free Hit's current-squad baseline doesn't reuse autosub either" —
autosub is inherently a reaction to a SPECIFIC gameweek's late injury news, so
pre-guessing which future candidate weeks would trigger one is spurious
precision a coarse "scenario ranking, not perfect optimization" layer
shouldn't attempt. One concrete consequence: each starter/bench player's own
raw ``multiplier`` field (whatever it happened to be when this snapshot was
taken) is never read for gain purposes — Bench Boost's gain is bench-only
(the starters' multiplier cancels out algebraically, see
``app.chips.scenario._paired_bench_boost_gain_samples``) and Triple Captain's
``captain_pool`` entries always get ``multiplier=1`` per that dataclass's own
contract (the evaluator applies its own captain multiplier internally) — so
this simplification costs nothing in practice for either chip.

``sample_count`` on a persisted ``chip_window_candidates`` row: kept in sync
with, but not imported from, ``app.chips.scenario``'s own (private)
``_CHIP_SAMPLE_COUNT`` default -- this handler passes its own
``_MONTE_CARLO_SAMPLE_COUNT`` explicitly to ``evaluate_bench_boost_window``/
``evaluate_triple_captain_window`` rather than relying on an unexported
default, so the persisted value is always exactly what was actually used.
Wildcard/Free Hit candidates are deterministic single MIP solves (see that
module's own docstring) — ``sample_count`` is always ``NULL`` for those.

League-context refinement (Milestone 6 Layer 4 -- the final piece of this
milestone): for the IMMEDIATE candidate only (the very first entry in a
chip's evaluated ``results`` list -- the next gameweek whose deadline hasn't
passed), if the
user has a REAL, resolvable FPL manager identity and a real, scheduled H2H
opponent for that exact gameweek, ``_compute_league_context_h2h_probabilities``
runs a paired Monte Carlo win-probability comparison (with vs. without this
one chip) against that opponent's own predicted state, reusing Milestone 5's
own ``app.h2h.simulate`` primitives (``sample_rival_scores``/
``sample_user_scores``/``summarize_matchup``) exactly like ``H2HSimulationJobRunner``
does. The two resulting probabilities are persisted on that ONE
``chip_window_candidates`` row only (``h2h_win_probability_with_chip``/
``h2h_win_probability_without_chip``) — every other candidate stays ``NULL``
(a future gameweek's real opponent is never knowable in advance).

Resolution chain, every step of which degrades gracefully to "skip this
refinement, leave both columns NULL" rather than ever failing the run:

1. ``ManagerSquadSnapshot.fpl_manager_id`` must be non-NULL -- a
   ``MANUAL_ENTRY`` snapshot (this codebase's own hand-built/hypothetical
   squads, ``fpl_manager_id=NULL`` by construction) has no real FPL manager
   behind it, so there is no opponent to look up at all. This is exactly the
   same field ``MANAGER_SYNC`` (``app/jobs/handlers/manager_sync.py``) already
   stamps onto every real synced snapshot -- no new job parameter is needed
   to answer "which real manager is this".
2. An ``h2h_matches`` row where ``event == target_fpl_event_id`` and either
   ``entry_1_id``/``entry_2_id`` equals that ``fpl_manager_id`` -- the OTHER
   entry is the real opponent's raw FPL manager id. No league filter is
   applied (a manager's real FPL manager id is globally unique across every
   league FPL itself tracks, so this is unambiguous without also matching a
   specific internal ``league_id``).
3. A ``rival_profiles`` row for that opponent (``season_id`` + their
   ``fpl_manager_id``) with a non-NULL ``last_known_snapshot_id`` whose
   ``rival_gameweek_snapshots`` row has usable ``raw_picks_json`` -- the same
   "most recent AVAILABLE snapshot" resolution ``H2HSimulationJobRunner``
   already performs, reused verbatim in shape (not imported -- that job's own
   helpers are private to its module, same duplication precedent as
   ``_split_starting_and_bench`` below).
4. (Wildcard/Free Hit only) a real, ``OPTIMAL`` rebuilt-squad
   ``JointSquadSolution`` for the immediate candidate (see
   ``app.chips.scenario.ChipWindowCandidateResult.solution``, added
   additively in this same layer) -- an INFEASIBLE/ERROR rebuild has no
   with-chip squad to compare.

Rival-side construction deliberately does NOT run ``H2HSimulationJobRunner``'s
own ``_resolve_effective_starting_xi`` real-FPL autosub (that function is
private to that module, and this handler already deliberately skips autosub
for the user's OWN side too across its general multi-week chip evaluation --
see this docstring's "Starting-XI/bench/captain resolution" section above).
Extending that same simplification to the rival side here, for this one
narrowly-scoped single-gameweek refinement, keeps this addition genuinely
small; a rival starter who happens to be flagged unavailable this one
gameweek is projected at whatever ``project_player_with_champion`` already
returns for them (which itself down-weights an unavailable player), rather
than being swapped for a bench replacement. A documented Layer 4
simplification, not a silent gap.

Scope boundary -- this signal is SUPPLEMENTARY, transparency-only context,
never a new input to the recommendation algorithm itself:
``rank_candidates_for_chip`` (Layer 2) is entirely unchanged and still ranks
USE_NOW/HOLD purely on ``gain_mean`` -- a real opponent's win probability is
surfaced alongside the recommendation for situational awareness, never folded
into which chip-timing decision gets recommended.
"""

import logging
import random
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.chips.inventory import UserChipWindow, derive_user_chip_windows
from app.chips.scenario import (
    BenchBoostGameweekCandidate,
    BenchPlayerInput,
    ChipWindowCandidateResult,
    RecommendationResult,
    TripleCaptainGameweekCandidate,
    _rebuild_budget,
    evaluate_bench_boost_window,
    evaluate_free_hit,
    evaluate_triple_captain_window,
    evaluate_wildcard,
    rank_candidates_for_chip,
    top_captain_candidates,
)
from app.chips.windows import ChipWindow, resolve_chip_windows
from app.h2h.simulate import (
    RivalHeldPlayerInput,
    RivalTransferTargetInput,
    SimulationInputs,
    UserPlayerInput,
    sample_rival_scores,
    sample_user_scores,
    summarize_matchup,
)
from app.jobs.handlers.lineup_optimization import _build_lineup_candidates
from app.jobs.registry import register_handler
from app.jobs.runner import JobContext, JobResult, JobRunner
from app.optimization.champion_loader import LoadedChampion, load_current_champion
from app.optimization.constraints import (
    OptimizationConstraints,
    RulesBlockedError,
    get_captain_multiplier,
    load_optimization_constraints,
)
from app.optimization.fixture_calendar import (
    resolve_team_fixture_counts_for_gameweek,
    resolve_team_fixture_counts_for_gameweeks,
)
from app.optimization.gameweek_resolution import (
    resolve_gameweeks_within_window,
    resolve_most_recent_finished_gameweek_event,
    resolve_target_gameweek,
)
from app.optimization.gate import GateResult, check_optimization_gate
from app.optimization.lineup_solver import solve_lineup
from app.optimization.projection import PROJECTION_FORMULA_VERSION, project_player_with_champion
from app.optimization.residual_cache import _ResidualCache
from packages.database.models.chip_optimization_run import ChipOptimizationRun
from packages.database.models.chip_recommendation import ChipRecommendation
from packages.database.models.chip_window_candidate import ChipWindowCandidate
from packages.database.models.gameweek import Gameweek
from packages.database.models.h2h_match import H2HMatch
from packages.database.models.manager_squad_snapshot import ManagerSquadSnapshot
from packages.database.models.player import Player
from packages.database.models.position import Position
from packages.database.models.rival_gameweek_snapshot import RivalGameweekSnapshot
from packages.database.models.rival_profile import RivalProfile
from packages.database.models.rules_config import RulesConfig
from packages.database.session import SessionLocal
from packages.domain.chips import ChipOptimizationRunStatus, ChipType, ChipVerdict, UserChipWindowStatus
from packages.domain.jobs import JobStatus, JobType
from packages.domain.optimization import SolverStatus, TargetGameweekStatus

logger = logging.getLogger(__name__)

_STARTING_XI_MAX_POSITION = 11

# See module docstring's "sample_count" section -- passed explicitly to the two Monte Carlo
# evaluators so the persisted value always matches what was actually used.
_MONTE_CARLO_SAMPLE_COUNT = 2000


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


def _resolve_requested_chip_types(parameters: dict | None) -> list[ChipType]:
    """Every ``ChipType`` (default), or exactly the caller-requested subset via ``parameters.chip_types``."""
    raw = (parameters or {}).get("chip_types")
    if raw is None:
        return list(ChipType)
    resolved: list[ChipType] = []
    for raw_type in raw:
        try:
            resolved.append(ChipType(raw_type))
        except ValueError:
            raise _InputResolutionError(
                "INVALID_CHIP_TYPE", f"Unrecognized chip_type '{raw_type}'."
            ) from None
    return resolved


def _split_starting_and_bench(raw_picks_json: dict | None) -> tuple[list[dict], list[dict]]:
    """Split a picks payload into (starting XI, bench-in-priority-order). Mirrors
    ``app.jobs.handlers.h2h_simulation``'s own private helper of the same name (duplicated, not
    imported -- that function is private to its own module, same precedent as
    ``app.chips.scenario._build_full_candidate_pool`` duplicating ``squad_optimization``'s own).
    """
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
    """Fallback for a snapshot with no usable ``raw_picks_json`` (MANUAL_ENTRY squads never have
    one -- see ``ManagerSquadSnapshot``'s own docstring; a MANAGER_SYNC snapshot could in principle
    lack one too, if that run's own picks fetch failed).

    A manually-entered squad was never assigned a real starting XI/bench/captain by an actual
    human, so there is nothing in ``raw_picks_json`` to reuse verbatim (see this module's own
    docstring on why every OTHER caller reads it verbatim, never re-derives it) -- the honest
    substitute is the SAME solver ``LINEUP_OPTIMIZATION`` itself uses
    (``app.optimization.lineup_solver.solve_lineup``, via the same
    ``app.jobs.handlers.lineup_optimization._build_lineup_candidates`` candidate-building this
    module reuses directly rather than duplicating), reshaped into the identical picks-dict shape
    ``_split_starting_and_bench`` produces so every downstream consumer here is unchanged.
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


def _resolve_captain_element(starters: list[dict]) -> int | None:
    captain_pick = next((p for p in starters if p.get("is_captain")), None)
    return captain_pick["element"] if captain_pick is not None else None


def _current_squad_starting_ids_and_captain(
    starters: list[dict], player_by_element: dict[int, Player]
) -> tuple[list[uuid.UUID], uuid.UUID]:
    starting_player_ids = [
        player_by_element[p["element"]].id for p in starters if p["element"] in player_by_element
    ]
    if not starting_player_ids:
        raise _SimulationBuildError("None of the starting XI elements resolved to current Player rows.")

    captain_element = _resolve_captain_element(starters)
    captain_player = player_by_element.get(captain_element) if captain_element is not None else None
    if captain_player is None:
        raise _SimulationBuildError("The captain pick could not be resolved to a current Player row.")
    return starting_player_ids, captain_player.id


def _fpl_event_ids_by_gameweek(session: Session, gameweek_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    rows = session.execute(
        select(Gameweek.id, Gameweek.fpl_event_id).where(Gameweek.id.in_(gameweek_ids))
    ).all()
    return {gw_id: fpl_event_id for gw_id, fpl_event_id in rows}


def _build_bench_boost_candidates(
    session: Session,
    season_id: uuid.UUID,
    starters: list[dict],
    bench: list[dict],
    window_gameweek_ids: list[uuid.UUID],
    champion: LoadedChampion | None,
    residuals: _ResidualCache,
    positions_by_id: dict[uuid.UUID, str],
    player_by_element: dict[int, Player],
) -> list[BenchBoostGameweekCandidate]:
    event_by_gameweek = _fpl_event_ids_by_gameweek(session, window_gameweek_ids)
    candidates: list[BenchBoostGameweekCandidate] = []
    for gameweek_id in window_gameweek_ids:
        starter_inputs: list[UserPlayerInput] = []
        for pick in starters:
            player = player_by_element.get(pick["element"])
            if player is None:
                continue
            projection = project_player_with_champion(session, player, gameweek_id, champion)
            position_short_name = _position_short_name(positions_by_id, player)
            # multiplier is irrelevant to Bench Boost's gain (see module docstring) -- 1 always.
            starter_inputs.append(
                UserPlayerInput(
                    player_id=str(player.id),
                    projected_points=projection.projected_points,
                    multiplier=1,
                    own_residuals=residuals.for_player(player.id),
                    position_residuals=residuals.for_position(player.position_id, position_short_name),
                )
            )
        bench_inputs: list[BenchPlayerInput] = []
        for pick in bench:
            player = player_by_element.get(pick["element"])
            if player is None or player.team_id is None:
                continue  # unresolved element, or a player with no resolved team -- skip, never crash
            projection = project_player_with_champion(session, player, gameweek_id, champion)
            position_short_name = _position_short_name(positions_by_id, player)
            bench_inputs.append(
                BenchPlayerInput(
                    player_id=str(player.id),
                    team_id=player.team_id,
                    projected_points=projection.projected_points,
                    own_residuals=residuals.for_player(player.id),
                    position_residuals=residuals.for_position(player.position_id, position_short_name),
                )
            )
        if not starter_inputs or not bench_inputs:
            continue  # nothing resolvable for this gameweek -- skip rather than crash
        team_fixture_counts = resolve_team_fixture_counts_for_gameweek(session, season_id, gameweek_id)
        candidates.append(
            BenchBoostGameweekCandidate(
                fpl_event_id=event_by_gameweek[gameweek_id],
                gameweek_id=gameweek_id,
                starters=tuple(starter_inputs),
                bench=tuple(bench_inputs),
                team_fixture_counts=team_fixture_counts,
            )
        )
    return candidates


def _build_triple_captain_candidates(
    session: Session,
    starters: list[dict],
    window_gameweek_ids: list[uuid.UUID],
    champion: LoadedChampion | None,
    residuals: _ResidualCache,
    positions_by_id: dict[uuid.UUID, str],
    player_by_element: dict[int, Player],
) -> list[TripleCaptainGameweekCandidate]:
    event_by_gameweek = _fpl_event_ids_by_gameweek(session, window_gameweek_ids)
    candidates: list[TripleCaptainGameweekCandidate] = []
    for gameweek_id in window_gameweek_ids:
        starter_inputs: list[UserPlayerInput] = []
        for pick in starters:
            player = player_by_element.get(pick["element"])
            if player is None:
                continue
            projection = project_player_with_champion(session, player, gameweek_id, champion)
            position_short_name = _position_short_name(positions_by_id, player)
            starter_inputs.append(
                UserPlayerInput(
                    player_id=str(player.id),
                    projected_points=projection.projected_points,
                    multiplier=1,
                    own_residuals=residuals.for_player(player.id),
                    position_residuals=residuals.for_position(player.position_id, position_short_name),
                )
            )
        if not starter_inputs:
            continue
        candidates.append(
            TripleCaptainGameweekCandidate(
                fpl_event_id=event_by_gameweek[gameweek_id],
                gameweek_id=gameweek_id,
                captain_pool=top_captain_candidates(tuple(starter_inputs)),
            )
        )
    return candidates


# --- Per-chip window selection / verdict mapping for a non-evaluable window -------------------

_NO_EVALUATION_VERDICT_BY_STATUS: dict[UserChipWindowStatus, ChipVerdict] = {
    UserChipWindowStatus.USED: ChipVerdict.ALREADY_USED,
    UserChipWindowStatus.ACTIVE_THIS_GAMEWEEK: ChipVerdict.UNAVAILABLE,
    UserChipWindowStatus.EXPIRED_UNUSED: ChipVerdict.EXPIRED_UNUSED,
}


def _select_current_window(
    derived_windows: list[UserChipWindow], target_fpl_event_id: int
) -> UserChipWindow | None:
    """The one window (of a chip's typically two) whose ``[start_event, stop_event]`` contains
    ``target_fpl_event_id`` -- see module docstring's "One chip type, one recommendation" section.
    """
    return next(
        (
            uc
            for uc in derived_windows
            if uc.window.start_event <= target_fpl_event_id <= uc.window.stop_event
        ),
        None,
    )


def _no_evaluation_rationale(
    status: UserChipWindowStatus, window: ChipWindow, used_at_fpl_event_id: int | None
) -> str:
    if status == UserChipWindowStatus.USED:
        return f"Already used at GW{used_at_fpl_event_id} (window {window.window_number})."
    if status == UserChipWindowStatus.ACTIVE_THIS_GAMEWEEK:
        return f"Active this gameweek (window {window.window_number}) -- already committed, not a decision."
    return f"Window {window.window_number} (GW{window.start_event}-{window.stop_event}) expired unused."


@dataclass(frozen=True)
class _ChipEvaluationContext:
    """Every already-resolved input a single chip's evaluation needs -- built once per job run, then
    reused unchanged for each requested ``ChipType``.
    """

    season_id: uuid.UUID
    starters: list[dict]
    bench: list[dict]
    player_by_element: dict[int, Player]
    positions_by_id: dict[uuid.UUID, str]
    residuals: _ResidualCache
    champion: LoadedChampion | None
    constraints: OptimizationConstraints
    captain_multiplier: int
    current_squad_starting_player_ids: list[uuid.UUID]
    current_squad_captain_player_id: uuid.UUID
    bank: int
    selling_prices: dict[uuid.UUID, int]
    rng: random.Random
    # Milestone 6 Layer 4 (league-context refinement): the real FPL manager id behind this
    # snapshot, straight off ManagerSquadSnapshot.fpl_manager_id -- None for a MANUAL_ENTRY
    # snapshot (no real manager behind it, see module docstring's resolution-chain step 1).
    fpl_manager_id: int | None
    # Single "as of" timestamp for this whole job run -- reused for
    # resolve_gameweeks_within_window's deadline-based filter rather than taking a second,
    # subtly different snapshot of "now" partway through evaluation.
    now: datetime


def _evaluate_chip_candidates(
    session: Session,
    chip_type: ChipType,
    window_gameweek_ids: list[uuid.UUID],
    ctx: _ChipEvaluationContext,
) -> list[ChipWindowCandidateResult]:
    """Dispatch to the right ``app.chips.scenario`` evaluator for one chip type's cost-appropriate
    candidate-building strategy -- see that module's own docstring for the Bench Boost/Triple
    Captain (exhaustive, cheap) vs Wildcard/Free Hit (capped, MIP-based) cost asymmetry.
    """
    if chip_type == ChipType.BBOOST:
        bb_candidates = _build_bench_boost_candidates(
            session,
            ctx.season_id,
            ctx.starters,
            ctx.bench,
            window_gameweek_ids,
            ctx.champion,
            ctx.residuals,
            ctx.positions_by_id,
            ctx.player_by_element,
        )
        return evaluate_bench_boost_window(bb_candidates, ctx.rng, sample_count=_MONTE_CARLO_SAMPLE_COUNT)

    if chip_type == ChipType.TRIPLE_CAPTAIN:
        tc_candidates = _build_triple_captain_candidates(
            session,
            ctx.starters,
            window_gameweek_ids,
            ctx.champion,
            ctx.residuals,
            ctx.positions_by_id,
            ctx.player_by_element,
        )
        return evaluate_triple_captain_window(
            tc_candidates, ctx.captain_multiplier, ctx.rng, sample_count=_MONTE_CARLO_SAMPLE_COUNT
        )

    if chip_type == ChipType.WILDCARD:
        return evaluate_wildcard(
            session,
            ctx.season_id,
            window_gameweek_ids,
            ctx.current_squad_starting_player_ids,
            ctx.current_squad_captain_player_id,
            ctx.bank,
            ctx.selling_prices,
            ctx.constraints,
            ctx.captain_multiplier,
            ctx.champion,
        )

    if chip_type == ChipType.FREEHIT:
        return evaluate_free_hit(
            session,
            ctx.season_id,
            window_gameweek_ids,
            ctx.current_squad_starting_player_ids,
            ctx.current_squad_captain_player_id,
            ctx.bank,
            ctx.selling_prices,
            ctx.constraints,
            ctx.captain_multiplier,
            ctx.champion,
        )

    raise AssertionError(
        f"unhandled chip_type in _evaluate_chip_candidates: {chip_type}"
    )  # ChipType is closed


# --- League-context refinement (Milestone 6 Layer 4) -- see module docstring for the full ------
# --- resolution chain, every graceful-degradation point, and the "never feeds into ranking" ----
# --- scope boundary. -----------------------------------------------------------------------------


def _find_real_opponent_match(
    session: Session, fpl_manager_id: int, target_fpl_event_id: int
) -> H2HMatch | None:
    """The real, scheduled ``h2h_matches`` row for this exact gameweek naming ``fpl_manager_id`` as
    either entry -- ``None`` if no such match exists (off-season, a bye week, a league never synced).
    """
    return (
        session.execute(
            select(H2HMatch).where(
                H2HMatch.event == target_fpl_event_id,
                or_(H2HMatch.entry_1_id == fpl_manager_id, H2HMatch.entry_2_id == fpl_manager_id),
            )
        )
        .scalars()
        .first()
    )


def _opponent_fpl_manager_id(match: H2HMatch, fpl_manager_id: int) -> int:
    return match.entry_2_id if match.entry_1_id == fpl_manager_id else match.entry_1_id


def _resolve_real_rival_state(
    session: Session, season_id: uuid.UUID, opponent_fpl_manager_id: int
) -> tuple[RivalProfile, RivalGameweekSnapshot] | None:
    """The opponent's synced ``RivalProfile`` + its most recent AVAILABLE ``RivalGameweekSnapshot``
    -- the same "last_known_snapshot_id" resolution ``H2HSimulationJobRunner`` already performs.
    ``None`` if the opponent was never synced, or has no usable last-known squad.
    """
    rival_profile = session.execute(
        select(RivalProfile).where(
            RivalProfile.season_id == season_id, RivalProfile.fpl_manager_id == opponent_fpl_manager_id
        )
    ).scalar_one_or_none()
    if rival_profile is None or rival_profile.last_known_snapshot_id is None:
        return None
    rival_snapshot = session.get(RivalGameweekSnapshot, rival_profile.last_known_snapshot_id)
    if (
        rival_snapshot is None
        or not rival_snapshot.raw_picks_json
        or not rival_snapshot.raw_picks_json.get("picks")
    ):
        return None
    return rival_profile, rival_snapshot


def _build_rival_held_players_no_autosub(
    session: Session,
    season_id: uuid.UUID,
    rival_snapshot: RivalGameweekSnapshot,
    target_gameweek_id: uuid.UUID,
    champion: LoadedChampion | None,
    residuals: _ResidualCache,
    positions_by_id: dict[uuid.UUID, str],
) -> list[RivalHeldPlayerInput] | None:
    """Rival starting XI -> ``RivalHeldPlayerInput``, deliberately WITHOUT real-FPL autosub -- see
    module docstring's "Rival-side construction" section for why this simplification is scoped to
    this one narrow refinement. ``None`` if nothing resolves (degrades gracefully).
    """
    rival_starters, _rival_bench = _split_starting_and_bench(rival_snapshot.raw_picks_json)
    if not rival_starters:
        return None
    player_by_element = _resolve_players_by_element(
        session, season_id, {p["element"] for p in rival_starters}
    )

    held: list[RivalHeldPlayerInput] = []
    for pick in rival_starters:
        player = player_by_element.get(pick["element"])
        if player is None:
            continue
        projection = project_player_with_champion(session, player, target_gameweek_id, champion)
        position_short_name = _position_short_name(positions_by_id, player)
        held.append(
            RivalHeldPlayerInput(
                player_id=str(player.id),
                fpl_element_id=player.fpl_element_id,
                position=position_short_name,
                projected_points=projection.projected_points,
                own_residuals=residuals.for_player(player.id),
                position_residuals=residuals.for_position(player.position_id, position_short_name),
            )
        )
    return held or None


def _build_rival_transfer_targets(
    session: Session,
    season_id: uuid.UUID,
    candidates_json: list | None,
    target_gameweek_id: uuid.UUID,
    champion: LoadedChampion | None,
    residuals: _ResidualCache,
) -> list[RivalTransferTargetInput]:
    """Mirrors ``app.jobs.handlers.h2h_simulation``'s own private helper of the same name verbatim
    (duplicated, not imported -- that function is private to its own module, same precedent as
    ``_split_starting_and_bench`` above).
    """
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
            continue
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


def _user_players_for_ids(
    session: Session,
    player_ids: list[uuid.UUID],
    captain_player_id: uuid.UUID,
    captain_multiplier: int,
    target_gameweek_id: uuid.UUID,
    champion: LoadedChampion | None,
    residuals: _ResidualCache,
    positions_by_id: dict[uuid.UUID, str],
) -> list[UserPlayerInput]:
    """Resolve a starting-XI player-id list + captain into ``UserPlayerInput``s for ONE gameweek --
    shared by both the current-squad baseline and a Wildcard/Free Hit rebuilt-squad candidate.
    """
    result: list[UserPlayerInput] = []
    for player_id in player_ids:
        player = session.get(Player, player_id)
        if player is None:
            continue  # degrades gracefully -- same convention as every other player-id resolution here
        projection = project_player_with_champion(session, player, target_gameweek_id, champion)
        position_short_name = _position_short_name(positions_by_id, player)
        result.append(
            UserPlayerInput(
                player_id=str(player.id),
                projected_points=projection.projected_points,
                multiplier=captain_multiplier if player_id == captain_player_id else 1,
                own_residuals=residuals.for_player(player.id),
                position_residuals=residuals.for_position(player.position_id, position_short_name),
            )
        )
    return result


def _immediate_without_chip_user_players(
    session: Session, ctx: _ChipEvaluationContext, target_gameweek_id: uuid.UUID
) -> list[UserPlayerInput]:
    """The user's ACTUAL current starting XI/captain for the immediate gameweek, no chip applied --
    the one constant "without chip" baseline shared by every chip type's league-context comparison.
    """
    return _user_players_for_ids(
        session,
        ctx.current_squad_starting_player_ids,
        ctx.current_squad_captain_player_id,
        ctx.captain_multiplier,
        target_gameweek_id,
        ctx.champion,
        ctx.residuals,
        ctx.positions_by_id,
    )


def _immediate_with_chip_user_players(
    session: Session,
    chip_type: ChipType,
    target_gameweek_id: uuid.UUID,
    immediate_result: ChipWindowCandidateResult,
    without_chip_players: list[UserPlayerInput],
    ctx: _ChipEvaluationContext,
) -> list[UserPlayerInput] | None:
    """The user's squad/XI WITH this one chip's effect applied, for the SAME immediate gameweek.

    Bench Boost: the current XI plus all 4 bench players (multiplier 1 each) -- all 15 count.
    Triple Captain: the current XI unchanged, except the current captain's multiplier is bumped
    from ``captain_multiplier`` to ``captain_multiplier + 1``.
    Wildcard/Free Hit: an entirely different squad -- the winning rebuilt-squad solution this exact
    candidate's own MIP solve already produced (``immediate_result.solution``, see
    ``app.chips.scenario.ChipWindowCandidateResult`` docstring). Returns ``None`` when that solution
    is unavailable (an INFEASIBLE/ERROR rebuild never attaches one) -- the caller skips this
    refinement entirely in that case, same as every other graceful-degradation path here.
    """
    if chip_type == ChipType.BBOOST:
        bench_players: list[UserPlayerInput] = []
        for pick in ctx.bench:
            player = ctx.player_by_element.get(pick["element"])
            if player is None:
                continue
            projection = project_player_with_champion(session, player, target_gameweek_id, ctx.champion)
            position_short_name = _position_short_name(ctx.positions_by_id, player)
            bench_players.append(
                UserPlayerInput(
                    player_id=str(player.id),
                    projected_points=projection.projected_points,
                    multiplier=1,
                    own_residuals=ctx.residuals.for_player(player.id),
                    position_residuals=ctx.residuals.for_position(player.position_id, position_short_name),
                )
            )
        return without_chip_players + bench_players

    if chip_type == ChipType.TRIPLE_CAPTAIN:
        boosted_multiplier = ctx.captain_multiplier + 1
        captain_player_id_str = str(ctx.current_squad_captain_player_id)
        return [
            replace(p, multiplier=boosted_multiplier) if p.player_id == captain_player_id_str else p
            for p in without_chip_players
        ]

    if chip_type in (ChipType.WILDCARD, ChipType.FREEHIT):
        solution = immediate_result.solution
        if solution is None or solution.captain_player_id is None:
            return None
        return _user_players_for_ids(
            session,
            solution.starting_player_ids,
            solution.captain_player_id,
            ctx.captain_multiplier,
            target_gameweek_id,
            ctx.champion,
            ctx.residuals,
            ctx.positions_by_id,
        )

    raise AssertionError(
        f"unhandled chip_type in _immediate_with_chip_user_players: {chip_type}"
    )  # ChipType is closed


def _compute_league_context_h2h_probabilities(
    session: Session,
    chip_type: ChipType,
    target_fpl_event_id: int,
    target_gameweek_id: uuid.UUID,
    immediate_result: ChipWindowCandidateResult,
    ctx: _ChipEvaluationContext,
) -> tuple[Decimal, Decimal] | None:
    """The league-context refinement itself: ``(win_probability_with_chip, win_probability_without_chip)``
    for the IMMEDIATE candidate against the user's real, scheduled H2H opponent -- or ``None`` if any
    step of the resolution chain (module docstring) comes up empty. NEVER raises: any unexpected
    failure here is caught and logged, never allowed to fail the chip recommendation itself -- this
    is a supplementary, transparency-only signal, not a required input.
    """
    if ctx.fpl_manager_id is None:
        return None
    try:
        match = _find_real_opponent_match(session, ctx.fpl_manager_id, target_fpl_event_id)
        if match is None:
            return None
        opponent_fpl_manager_id = _opponent_fpl_manager_id(match, ctx.fpl_manager_id)
        rival_state = _resolve_real_rival_state(session, ctx.season_id, opponent_fpl_manager_id)
        if rival_state is None:
            return None
        rival_profile, rival_snapshot = rival_state

        rival_held_players = _build_rival_held_players_no_autosub(
            session,
            ctx.season_id,
            rival_snapshot,
            target_gameweek_id,
            ctx.champion,
            ctx.residuals,
            ctx.positions_by_id,
        )
        if rival_held_players is None:
            return None
        rival_transfer_targets = _build_rival_transfer_targets(
            session,
            ctx.season_id,
            rival_profile.predicted_likely_transfer_targets_json,
            target_gameweek_id,
            ctx.champion,
            ctx.residuals,
        )

        without_chip_players = _immediate_without_chip_user_players(session, ctx, target_gameweek_id)
        with_chip_players = _immediate_with_chip_user_players(
            session, chip_type, target_gameweek_id, immediate_result, without_chip_players, ctx
        )
        if not without_chip_players or not with_chip_players:
            return None

        base_inputs = SimulationInputs(
            user_players=(),  # unused by sample_rival_scores -- each side's own players are sampled below
            rival_held_players=tuple(rival_held_players),
            rival_transfer_targets=tuple(rival_transfer_targets),
            rival_no_transfer_probability=float(rival_profile.predicted_no_transfer_probability or 0),
            rival_one_transfer_probability=float(rival_profile.predicted_one_transfer_probability or 0),
            rival_multi_transfer_probability=float(rival_profile.predicted_multi_transfer_probability or 0),
            rival_captain_probabilities=dict(rival_profile.predicted_captain_probabilities_json or {}),
            rival_state_confidence=rival_profile.overall_confidence,
        )

        # Same "one shared, precomputed rival distribution reused for a fair, paired comparison"
        # principle as sample_rival_scores' own docstring -- both the with-chip and without-chip
        # user-side samples are compared against the exact SAME rival_scores tuple.
        rng = random.Random()
        rival_scores = sample_rival_scores(base_inputs, rng)
        without_chip_scores = sample_user_scores(tuple(without_chip_players), rng, base_inputs.sample_count)
        with_chip_scores = sample_user_scores(tuple(with_chip_players), rng, base_inputs.sample_count)

        without_chip_summary = summarize_matchup(
            without_chip_scores, rival_scores, rival_profile.overall_confidence
        )
        with_chip_summary = summarize_matchup(
            with_chip_scores, rival_scores, rival_profile.overall_confidence
        )

        return (
            Decimal(str(round(with_chip_summary.win_probability, 4))),
            Decimal(str(round(without_chip_summary.win_probability, 4))),
        )
    except Exception:  # noqa: BLE001 -- supplementary signal only, must never block the recommendation
        logger.warning(
            "chip_simulation: league-context H2H refinement failed for chip_type=%s "
            "target_fpl_event_id=%s; leaving h2h_win_probability_* NULL for this candidate.",
            chip_type.value,
            target_fpl_event_id,
            exc_info=True,
        )
        return None


# --- Sample count persisted per candidate (see module docstring) -------------------------------

_SAMPLE_COUNT_BY_CHIP_TYPE: dict[ChipType, int | None] = {
    ChipType.BBOOST: _MONTE_CARLO_SAMPLE_COUNT,
    ChipType.TRIPLE_CAPTAIN: _MONTE_CARLO_SAMPLE_COUNT,
    ChipType.WILDCARD: None,
    ChipType.FREEHIT: None,
}


def _persist_candidates_and_recommendation(
    session: Session,
    run_id: uuid.UUID,
    chip_type: ChipType,
    window_number: int,
    season_id: uuid.UUID,
    results: list[ChipWindowCandidateResult],
    recommendation: RecommendationResult,
    immediate_h2h_win_probabilities: tuple[Decimal, Decimal] | None = None,
) -> None:
    """``immediate_h2h_win_probabilities``, if not ``None``, is ``(with_chip, without_chip)`` and is
    applied ONLY to ``results[0]``'s own persisted row (the immediate candidate) -- see module
    docstring's "League-context refinement" section. Every other candidate row always persists both
    new columns as ``NULL``.
    """
    gameweek_ids = [r.gameweek_id for r in results]
    counts_by_gameweek = resolve_team_fixture_counts_for_gameweeks(session, season_id, gameweek_ids)
    rank_by_event = {r.fpl_event_id: i + 1 for i, r in enumerate(sorted(results, key=lambda r: -r.gain_mean))}
    sample_count = _SAMPLE_COUNT_BY_CHIP_TYPE[chip_type]
    immediate_result = results[0]

    candidate_id_by_event: dict[int, uuid.UUID] = {}
    for result in results:
        counts = counts_by_gameweek.get(result.gameweek_id, {})
        is_blank = any(c.fixture_count == 0 for c in counts.values()) if counts else None
        is_double = any(c.fixture_count >= 2 for c in counts.values()) if counts else None
        if immediate_h2h_win_probabilities is not None and result is immediate_result:
            with_chip_probability, without_chip_probability = immediate_h2h_win_probabilities
        else:
            with_chip_probability, without_chip_probability = None, None
        row = ChipWindowCandidate(
            chip_optimization_run_id=run_id,
            chip_type=chip_type,
            window_number=window_number,
            candidate_fpl_event_id=result.fpl_event_id,
            is_blank_gameweek=is_blank,
            is_double_gameweek=is_double,
            gain_mean=Decimal(str(round(result.gain_mean, 2))),
            gain_floor=Decimal(str(round(result.gain_floor, 2))),
            gain_ceiling=Decimal(str(round(result.gain_ceiling, 2))),
            sample_count=sample_count,
            rank_within_chip_type=rank_by_event[result.fpl_event_id],
            h2h_win_probability_with_chip=with_chip_probability,
            h2h_win_probability_without_chip=without_chip_probability,
        )
        session.add(row)
        session.flush()  # populate row.id for the recommendation's own FK, below
        candidate_id_by_event[result.fpl_event_id] = row.id

    recommended_candidate_id = (
        candidate_id_by_event.get(recommendation.recommended_fpl_event_id)
        if recommendation.recommended_fpl_event_id is not None
        else None
    )
    session.add(
        ChipRecommendation(
            chip_optimization_run_id=run_id,
            chip_type=chip_type,
            verdict=recommendation.verdict,
            recommended_candidate_id=recommended_candidate_id,
            rationale=recommendation.rationale,
        )
    )


def _bench_zero_minute_player_names(bench: list[dict], player_by_element: dict[int, Player]) -> list[str]:
    """Real bench players (by ``web_name``) with 0 minutes played this season so far.

    Real gap this closes (found live, 2026-09-17): Bench Boost's own Monte Carlo ``gain_mean``
    already correctly reflects a genuine zero-minute player's near-zero individual projection
    (``project_player_with_champion`` naturally produces a low number for them) -- but a bench
    of 4 can still show a superficially reasonable positive average gain while resting on one or
    more players who realistically have never been selected to play at all this season, which a
    single blended mean doesn't separately call out. Resolves straight off each pick's real
    ``Player`` row; a pick whose element doesn't resolve is skipped (never crashes), mirroring
    every other resolution loop in this module.
    """
    names = []
    for pick in bench:
        player = player_by_element.get(pick["element"])
        if player is not None and player.minutes == 0:
            names.append(player.web_name or f"element {player.fpl_element_id}")
    return names


def _downgrade_bench_boost_for_unreliable_bench(
    session: Session, chip_type: ChipType, ctx: _ChipEvaluationContext, recommendation: RecommendationResult
) -> RecommendationResult:
    """Downgrades a Bench Boost ``USE_NOW`` verdict to ``HOLD`` when the CURRENT real bench (the
    one that would actually be used if boosted right now) has one or more players with 0 real
    minutes played this season -- see ``_bench_zero_minute_player_names`` for the real gap this
    closes. Scoped to BBOOST only (the only chip with a literal "bench" concept) and to a genuine
    ``USE_NOW`` verdict for the IMMEDIATE gameweek specifically (``rank_candidates_for_chip``'s own
    contract guarantees ``USE_NOW``'s ``recommended_fpl_event_id`` is always the immediate week --
    a HOLD-for-later verdict already reflects a DIFFERENT future gameweek, whose real bench
    composition by then isn't something this check can honestly speak to, so it's left alone).

    Gated on the season having genuinely started (mirrors
    ``app.jobs.handlers.squad_optimization._build_candidates``'s own season-has-started-gated
    0-minutes exclusion, added 2026-09-05, for the identical real reason) -- pre-season, every
    real player genuinely has 0 minutes, so this check would otherwise fire on every single
    Bench Boost evaluation for a reason that isn't actually informative yet.

    Returns ``recommendation`` completely unchanged for every other case (not BBOOST, not
    USE_NOW, pre-season, or a bench with no genuinely zero-minute player) -- this function is a
    pure, no-op-by-default refinement, never a required step.
    """
    if chip_type != ChipType.BBOOST or recommendation.verdict != ChipVerdict.USE_NOW:
        return recommendation
    if resolve_most_recent_finished_gameweek_event(session, ctx.season_id) is None:
        return recommendation

    unreliable = _bench_zero_minute_player_names(ctx.bench, ctx.player_by_element)
    if not unreliable:
        return recommendation

    names = ", ".join(unreliable)
    plural = len(unreliable) != 1
    return RecommendationResult(
        verdict=ChipVerdict.HOLD,
        recommended_fpl_event_id=None,
        recommended_gain_mean=None,
        rationale=(
            f"Hold: {len(unreliable)} real bench player{'s' if plural else ''} ({names}) "
            f"{'have' if plural else 'has'} played 0 minutes this season. Bench Boost's own gain "
            "estimate already reflects their near-zero individual projection, but a bench this "
            "unreliable is not worth spending a chip on regardless of the raw estimated average."
        ),
    )


def _process_chip(
    session: Session,
    run_id: uuid.UUID,
    chip_type: ChipType,
    chip_windows_for_type: list[ChipWindow],
    chips_used_json: list[dict] | None,
    active_chip: str | None,
    target_fpl_event_id: int,
    ctx: _ChipEvaluationContext,
) -> None:
    """Evaluate (or skip) one chip type, persisting exactly one ``ChipRecommendation`` row (plus,
    for a genuinely ``AVAILABLE`` current window, one ``ChipWindowCandidate`` per evaluated
    candidate gameweek) -- see module docstring for the full verdict-mapping rationale.
    """
    derived = derive_user_chip_windows(
        chip_windows_for_type, chips_used_json, active_chip, target_fpl_event_id
    )
    current = _select_current_window(derived, target_fpl_event_id)

    if current is None:
        session.add(
            ChipRecommendation(
                chip_optimization_run_id=run_id,
                chip_type=chip_type,
                verdict=ChipVerdict.NO_VIABLE_WINDOW,
                recommended_candidate_id=None,
                rationale=f"No scheduled window covers GW{target_fpl_event_id}.",
            )
        )
        return

    if current.status != UserChipWindowStatus.AVAILABLE:
        verdict = _NO_EVALUATION_VERDICT_BY_STATUS[current.status]
        session.add(
            ChipRecommendation(
                chip_optimization_run_id=run_id,
                chip_type=chip_type,
                verdict=verdict,
                recommended_candidate_id=None,
                rationale=_no_evaluation_rationale(
                    current.status, current.window, current.used_at_fpl_event_id
                ),
            )
        )
        return

    window_gameweek_ids = resolve_gameweeks_within_window(
        session, ctx.season_id, current.window.stop_event, ctx.now
    )
    if not window_gameweek_ids:
        session.add(
            ChipRecommendation(
                chip_optimization_run_id=run_id,
                chip_type=chip_type,
                verdict=ChipVerdict.NO_VIABLE_WINDOW,
                recommended_candidate_id=None,
                rationale=f"No actionable gameweek remains in window {current.window.window_number}.",
            )
        )
        return

    results = _evaluate_chip_candidates(session, chip_type, window_gameweek_ids, ctx)
    if not results:
        session.add(
            ChipRecommendation(
                chip_optimization_run_id=run_id,
                chip_type=chip_type,
                verdict=ChipVerdict.NO_VIABLE_WINDOW,
                recommended_candidate_id=None,
                rationale=(
                    f"No candidate gameweek could be evaluated within window {current.window.window_number}."
                ),
            )
        )
        return

    # Milestone 6 Layer 4: the league-context refinement only ever considers results[0], the
    # IMMEDIATE candidate -- defensively re-checked against target_fpl_event_id since every
    # evaluator's own contract (see resolve_gameweeks_within_window/rank_candidates_for_chip
    # docstrings) already guarantees this equality for a genuinely AVAILABLE current window, but a
    # Wildcard/Free Hit rebuild that happened to be INFEASIBLE for the immediate week (excluded,
    # see evaluate_wildcard_candidate's own None-on-non-OPTIMAL convention) could otherwise leave
    # results[0] as a LATER candidate -- in which case this refinement is correctly skipped
    # entirely (never misapplied to the wrong candidate row).
    immediate_h2h_win_probabilities = None
    if results[0].fpl_event_id == target_fpl_event_id:
        immediate_h2h_win_probabilities = _compute_league_context_h2h_probabilities(
            session, chip_type, target_fpl_event_id, results[0].gameweek_id, results[0], ctx
        )

    recommendation = rank_candidates_for_chip(results)
    recommendation = _downgrade_bench_boost_for_unreliable_bench(session, chip_type, ctx, recommendation)
    _persist_candidates_and_recommendation(
        session,
        run_id,
        chip_type,
        current.window.window_number,
        ctx.season_id,
        results,
        recommendation,
        immediate_h2h_win_probabilities,
    )


@dataclass(frozen=True)
class _ChipEvaluationInputs:
    """The pure, pre-row-creation input-resolution phase, shared between ``ChipSimulationJobRunner``
    and ``ChipScheduleOptimizationJobRunner`` (Phase 2 of the joint chip scheduler, which imports
    this and ``_resolve_chip_evaluation_inputs``/``_build_chip_evaluation_context`` below directly
    -- mirrors this module's own existing precedent of importing ``_build_lineup_candidates`` from
    ``app.jobs.handlers.lineup_optimization`` across handler modules, rather than duplicating this
    much real business logic). Deliberately split from ``_ChipEvaluationContext`` (built by
    ``_build_chip_evaluation_context`` below): this half only ever raises ``_InputResolutionError``
    (safe to call before ANY row exists), while the context-building half can raise
    ``_SimulationBuildError`` (only safe to call once a caller's own RUNNING row already exists, so
    a build failure still finalizes a real FAILED audit row) -- keeping this split preserves each
    caller's own independent choice of exactly when its own row gets created relative to a possible
    failure, rather than silently changing ``ChipSimulationJobRunner``'s own existing behavior.
    """

    snapshot: ManagerSquadSnapshot
    season_id: uuid.UUID
    constraints: OptimizationConstraints
    rules_config: RulesConfig
    gate: GateResult
    target_gameweek_id: uuid.UUID
    target_fpl_event_id: int


def _resolve_chip_evaluation_inputs(
    session: Session, manager_squad_snapshot_id: uuid.UUID, now: datetime
) -> _ChipEvaluationInputs:
    """Snapshot lookup, optimization gate check, rules/constraints loading, and target-gameweek
    resolution -- every hard-refusal input check a chip evaluation needs, before any row exists.
    Raises ``_InputResolutionError`` for every refusal case; callers must catch this BEFORE
    creating their own run-header row (a refusal here must never pollute the audit trail).
    """
    snapshot = session.get(ManagerSquadSnapshot, manager_squad_snapshot_id)
    if snapshot is None:
        raise _InputResolutionError(
            "SNAPSHOT_NOT_FOUND", f"manager_squad_snapshots row {manager_squad_snapshot_id} not found."
        )
    season_id = snapshot.season_id

    gate = check_optimization_gate(session, season_id)
    if not gate.allowed:
        raise _InputResolutionError(gate.reason_code or "GATE_REFUSED", "Optimization gate refused.")

    try:
        constraints = load_optimization_constraints(session, season_id)
    except RulesBlockedError as exc:
        raise _InputResolutionError("RULES_BLOCKED", str(exc)) from exc

    rules_config = session.get(RulesConfig, constraints.rules_config_id)
    assert rules_config is not None  # rules_config_id was just resolved from a real row

    target_gameweek_id, status = resolve_target_gameweek(session, season_id, now)
    if status == TargetGameweekStatus.NO_ACTIVE_GAMEWEEK:
        raise _InputResolutionError(
            "NO_ACTIVE_GAMEWEEK",
            "No actionable gameweek (deadline not yet passed) exists to evaluate chip timing "
            "against (off-season).",
        )
    assert target_gameweek_id is not None
    target_gameweek = session.get(Gameweek, target_gameweek_id)
    assert target_gameweek is not None

    return _ChipEvaluationInputs(
        snapshot=snapshot,
        season_id=season_id,
        constraints=constraints,
        rules_config=rules_config,
        gate=gate,
        target_gameweek_id=target_gameweek_id,
        target_fpl_event_id=target_gameweek.fpl_event_id,
    )


def _build_chip_evaluation_context(
    session: Session, inputs: _ChipEvaluationInputs, now: datetime
) -> _ChipEvaluationContext:
    """Champion/residuals/captain-multiplier loading, starting-XI/bench resolution (real picks or
    the solved-lineup fallback), and every other input a chip evaluator itself needs -- built from
    an already-resolved ``_ChipEvaluationInputs``. Raises ``_SimulationBuildError`` -- only safe to
    call once the caller's own RUNNING row already exists (see ``_ChipEvaluationInputs``'s own
    docstring for why this is split from input resolution).
    """
    snapshot = inputs.snapshot
    season_id = inputs.season_id
    constraints = inputs.constraints

    champion = load_current_champion(session)
    residuals = _ResidualCache(session, season_id)
    captain_multiplier = get_captain_multiplier(session, constraints.rules_config_id)

    if snapshot.raw_picks_json and snapshot.raw_picks_json.get("picks"):
        starters, bench = _split_starting_and_bench(snapshot.raw_picks_json)
    else:
        starters, bench = _derive_starting_and_bench_from_solver(
            session, season_id, snapshot.squad_player_ids, inputs.target_gameweek_id, champion, constraints
        )
    if not starters:
        raise _SimulationBuildError(
            "Could not resolve a starting XI for this squad, neither from its own "
            "raw_picks_json nor by solving one -- see "
            "_derive_starting_and_bench_from_solver's own docstring for when this "
            "fallback itself can still come back empty (solver infeasibility)."
        )

    element_ids = {p["element"] for p in starters + bench}
    player_by_element = _resolve_players_by_element(session, season_id, element_ids)
    positions_by_id = _positions_by_id(session, season_id)

    current_squad_starting_player_ids, current_squad_captain_player_id = (
        _current_squad_starting_ids_and_captain(starters, player_by_element)
    )
    bank = snapshot.bank
    selling_prices = {uuid.UUID(k): v for k, v in snapshot.squad_player_selling_prices_json.items()}

    return _ChipEvaluationContext(
        season_id=season_id,
        starters=starters,
        bench=bench,
        player_by_element=player_by_element,
        positions_by_id=positions_by_id,
        residuals=residuals,
        champion=champion,
        constraints=constraints,
        captain_multiplier=captain_multiplier,
        current_squad_starting_player_ids=current_squad_starting_player_ids,
        current_squad_captain_player_id=current_squad_captain_player_id,
        bank=bank,
        selling_prices=selling_prices,
        rng=random.Random(),
        fpl_manager_id=snapshot.fpl_manager_id,
        now=now,
    )


def _post_wildcard_evaluation_context(
    session: Session, base_ctx: _ChipEvaluationContext, wildcard_result: ChipWindowCandidateResult
) -> _ChipEvaluationContext:
    """Builds a NEW ``_ChipEvaluationContext`` reflecting the hypothetical squad state after
    playing Wildcard at ``wildcard_result``'s own candidate gameweek -- the joint chip scheduler's
    Phase 2 path-dependence hook (see ``app.jobs.handlers.chip_schedule_optimization``'s own module
    docstring): every OTHER chip's candidate gameweek that falls AFTER this one gets re-evaluated
    against THIS context instead of ``base_ctx``, since a real Wildcard permanently reshapes the
    squad for the rest of the season (unlike Free Hit, which reverts the following gameweek --
    never a path-dependence trigger, so this is only ever called for a WILDCARD candidate).

    Reuses ``wildcard_result.solution`` (a real ``JointSquadSolution``, already computed by
    ``evaluate_wildcard`` itself -- no re-solving) rather than re-deriving anything: this is pure
    translation from that MIP solution's own player-id-keyed shape into the raw-picks-dict shape
    ``_ChipEvaluationContext.starters``/``.bench`` need (element-id-keyed, matching real FPL's own
    ``raw_picks_json`` -- see ``_split_starting_and_bench``'s own docstring for why every OTHER
    caller reads this shape verbatim).

    New bank: the SAME total rebuild budget (``_rebuild_budget`` -- bank plus the old squad's own
    total selling value, exactly what ``evaluate_wildcard_candidate`` itself budgeted the rebuild
    against) minus the winning rebuild's own ``squad_total_cost``. New selling prices: each new
    squad player's own current ``now_cost`` -- a real, deliberate simplification (a hypothetically
    "just-bought" player's own selling price equals its purchase price until the market moves;
    this codebase has no real profit/loss-taken modeling for a squad that was never actually
    bought, so assuming zero profit taken is the honest, conservative choice, not an oversight).
    """
    solution = wildcard_result.solution
    assert solution is not None  # only ever called with a Wildcard candidate's own result
    assert solution.captain_player_id is not None  # OPTIMAL solves always set this

    starting_ids = solution.starting_player_ids
    bench_ids = solution.bench_player_ids_ordered
    all_ids = [*starting_ids, *bench_ids]
    players_by_id = {p.id: p for p in session.execute(select(Player).where(Player.id.in_(all_ids))).scalars()}

    def _pick(player_id: uuid.UUID, position: int) -> dict:
        player = players_by_id[player_id]
        return {
            "element": player.fpl_element_id,
            "position": position,
            "is_captain": player_id == solution.captain_player_id,
            "is_vice_captain": player_id == solution.vice_captain_player_id,
            "multiplier": 2 if player_id == solution.captain_player_id else 1,
        }

    starters = [_pick(pid, index + 1) for index, pid in enumerate(starting_ids) if pid in players_by_id]
    bench = [
        _pick(pid, _STARTING_XI_MAX_POSITION + 1 + index)
        for index, pid in enumerate(bench_ids)
        if pid in players_by_id
    ]

    element_ids = {p["element"] for p in starters + bench}
    player_by_element = _resolve_players_by_element(session, base_ctx.season_id, element_ids)

    total_rebuild_budget = _rebuild_budget(base_ctx.bank, base_ctx.selling_prices)
    new_bank = total_rebuild_budget - (solution.squad_total_cost or 0)
    new_selling_prices = {pid: players_by_id[pid].now_cost for pid in all_ids if pid in players_by_id}

    return replace(
        base_ctx,
        starters=starters,
        bench=bench,
        player_by_element=player_by_element,
        current_squad_starting_player_ids=starting_ids,
        current_squad_captain_player_id=solution.captain_player_id,
        bank=new_bank,
        selling_prices=new_selling_prices,
    )


@register_handler(JobType.CHIP_SIMULATION)
class ChipSimulationJobRunner(JobRunner):
    """Evaluates every requested chip's currently-open window and persists a recommendation each."""

    def run(self, ctx: JobContext) -> JobResult:
        session = SessionLocal()
        run_row: ChipOptimizationRun | None = None
        now = datetime.now(UTC)
        try:
            try:
                manager_squad_snapshot_id = _parse_uuid_parameter(ctx.parameters, "manager_squad_snapshot_id")
                # No upfront raw_picks_json gate here anymore: a snapshot without one (every
                # MANUAL_ENTRY squad, or a MANAGER_SYNC one whose own picks fetch failed) falls back
                # to solving a starting XI instead, once execution reaches that point below -- see
                # _derive_starting_and_bench_from_solver's own docstring.
                requested_chip_types = _resolve_requested_chip_types(ctx.parameters)
                inputs = _resolve_chip_evaluation_inputs(session, manager_squad_snapshot_id, now)
            except _InputResolutionError as exc:
                return JobResult(
                    status=JobStatus.FAILED, output=None, error_code=exc.error_code, error_message=str(exc)
                )

            try:
                started_at = now
                run_row = ChipOptimizationRun(
                    job_id=uuid.UUID(ctx.job_id),
                    manager_squad_snapshot_id=inputs.snapshot.id,
                    season_id=inputs.season_id,
                    rules_config_id=inputs.rules_config.id,
                    run_status=ChipOptimizationRunStatus.RUNNING,
                    target_fpl_event_id=inputs.target_fpl_event_id,
                    projection_formula_version=PROJECTION_FORMULA_VERSION,
                    rules_status_snapshot=inputs.gate.rules_status,
                    optimization_permitted_snapshot=inputs.gate.optimization_permitted,
                    data_quality_score_snapshot=(
                        Decimal(str(inputs.gate.data_quality_score))
                        if inputs.gate.data_quality_score is not None
                        else None
                    ),
                    parameters_json=ctx.parameters,
                    started_at=started_at,
                )
                session.add(run_row)
                session.commit()
                session.refresh(run_row)

                eval_ctx = _build_chip_evaluation_context(session, inputs, now)
                chip_windows = resolve_chip_windows(inputs.rules_config.raw_chips_json)

                for chip_type in requested_chip_types:
                    windows_for_type = [w for w in chip_windows if w.chip_type == chip_type]
                    _process_chip(
                        session,
                        run_row.id,
                        chip_type,
                        windows_for_type,
                        inputs.snapshot.chips_used_json,
                        inputs.snapshot.active_chip,
                        inputs.target_fpl_event_id,
                        eval_ctx,
                    )

                run_row.run_status = ChipOptimizationRunStatus.SUCCEEDED
                run_row.completed_at = datetime.now(UTC)
                session.commit()

                return JobResult(
                    status=JobStatus.SUCCEEDED,
                    output={
                        "chip_optimization_run_id": str(run_row.id),
                        "target_fpl_event_id": inputs.target_fpl_event_id,
                    },
                    error_code=None,
                    error_message=None,
                )
            except Exception as exc:  # noqa: BLE001 - the run row must always reach a terminal status
                session.rollback()
                self._finalize_failed(session, run_row)
                return JobResult(
                    status=JobStatus.FAILED,
                    output={"chip_optimization_run_id": str(run_row.id)} if run_row is not None else None,
                    error_code="CHIP_SIMULATION_FAILED",
                    error_message=str(exc),
                )
        finally:
            session.close()

    def _finalize_failed(self, session: Session, run_row: ChipOptimizationRun | None) -> None:
        if run_row is None or run_row.id is None:
            return
        try:
            fresh = session.get(ChipOptimizationRun, run_row.id)
            if fresh is not None and fresh.run_status == ChipOptimizationRunStatus.RUNNING:
                fresh.run_status = ChipOptimizationRunStatus.FAILED
                fresh.completed_at = datetime.now(UTC)
                session.commit()
        except Exception:  # noqa: BLE001 - best-effort finalization only
            session.rollback()
