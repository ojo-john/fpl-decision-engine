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
"""

import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.chips.inventory import UserChipWindow, derive_user_chip_windows
from app.chips.scenario import (
    BenchBoostGameweekCandidate,
    BenchPlayerInput,
    ChipWindowCandidateResult,
    RecommendationResult,
    TripleCaptainGameweekCandidate,
    evaluate_bench_boost_window,
    evaluate_free_hit,
    evaluate_triple_captain_window,
    evaluate_wildcard,
    rank_candidates_for_chip,
    top_captain_candidates,
)
from app.chips.windows import ChipWindow, resolve_chip_windows
from app.h2h.simulate import UserPlayerInput
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
from app.optimization.gameweek_resolution import resolve_gameweeks_within_window, resolve_target_gameweek
from app.optimization.gate import check_optimization_gate
from app.optimization.projection import PROJECTION_FORMULA_VERSION, project_player_with_champion
from app.optimization.residual_cache import _ResidualCache
from packages.database.models.chip_optimization_run import ChipOptimizationRun
from packages.database.models.chip_recommendation import ChipRecommendation
from packages.database.models.chip_window_candidate import ChipWindowCandidate
from packages.database.models.gameweek import Gameweek
from packages.database.models.manager_squad_snapshot import ManagerSquadSnapshot
from packages.database.models.player import Player
from packages.database.models.position import Position
from packages.database.models.rules_config import RulesConfig
from packages.database.session import SessionLocal
from packages.domain.chips import ChipOptimizationRunStatus, ChipType, ChipVerdict, UserChipWindowStatus
from packages.domain.jobs import JobStatus, JobType
from packages.domain.optimization import TargetGameweekStatus

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
) -> None:
    gameweek_ids = [r.gameweek_id for r in results]
    counts_by_gameweek = resolve_team_fixture_counts_for_gameweeks(session, season_id, gameweek_ids)
    rank_by_event = {r.fpl_event_id: i + 1 for i, r in enumerate(sorted(results, key=lambda r: -r.gain_mean))}
    sample_count = _SAMPLE_COUNT_BY_CHIP_TYPE[chip_type]

    candidate_id_by_event: dict[int, uuid.UUID] = {}
    for result in results:
        counts = counts_by_gameweek.get(result.gameweek_id, {})
        is_blank = any(c.fixture_count == 0 for c in counts.values()) if counts else None
        is_double = any(c.fixture_count >= 2 for c in counts.values()) if counts else None
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

    window_gameweek_ids = resolve_gameweeks_within_window(session, ctx.season_id, current.window.stop_event)
    if not window_gameweek_ids:
        session.add(
            ChipRecommendation(
                chip_optimization_run_id=run_id,
                chip_type=chip_type,
                verdict=ChipVerdict.NO_VIABLE_WINDOW,
                recommended_candidate_id=None,
                rationale=f"No unfinished gameweek remains in window {current.window.window_number}.",
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

    recommendation = rank_candidates_for_chip(results)
    _persist_candidates_and_recommendation(
        session, run_id, chip_type, current.window.window_number, ctx.season_id, results, recommendation
    )


@register_handler(JobType.CHIP_SIMULATION)
class ChipSimulationJobRunner(JobRunner):
    """Evaluates every requested chip's currently-open window and persists a recommendation each."""

    def run(self, ctx: JobContext) -> JobResult:
        session = SessionLocal()
        run_row: ChipOptimizationRun | None = None
        try:
            try:
                manager_squad_snapshot_id = _parse_uuid_parameter(ctx.parameters, "manager_squad_snapshot_id")
                snapshot = session.get(ManagerSquadSnapshot, manager_squad_snapshot_id)
                if snapshot is None:
                    raise _InputResolutionError(
                        "SNAPSHOT_NOT_FOUND",
                        f"manager_squad_snapshots row {manager_squad_snapshot_id} not found.",
                    )
                season_id = snapshot.season_id

                if not snapshot.raw_picks_json or not snapshot.raw_picks_json.get("picks"):
                    raise _InputResolutionError(
                        "SNAPSHOT_MISSING_PICKS_DATA",
                        "manager_squad_snapshot has no raw_picks_json to resolve a starting XI from.",
                    )

                requested_chip_types = _resolve_requested_chip_types(ctx.parameters)

                gate = check_optimization_gate(session, season_id)
                if not gate.allowed:
                    raise _InputResolutionError(
                        gate.reason_code or "GATE_REFUSED", "Optimization gate refused."
                    )

                try:
                    constraints = load_optimization_constraints(session, season_id)
                except RulesBlockedError as exc:
                    raise _InputResolutionError("RULES_BLOCKED", str(exc)) from exc

                rules_config = session.get(RulesConfig, constraints.rules_config_id)
                assert rules_config is not None  # rules_config_id was just resolved from a real row

                target_gameweek_id, status = resolve_target_gameweek(session, season_id)
                if status == TargetGameweekStatus.NO_ACTIVE_GAMEWEEK:
                    raise _InputResolutionError(
                        "NO_ACTIVE_GAMEWEEK",
                        "No unfinished gameweek exists to evaluate chip timing against (off-season).",
                    )
                assert target_gameweek_id is not None
                target_gameweek = session.get(Gameweek, target_gameweek_id)
                assert target_gameweek is not None
                target_fpl_event_id = target_gameweek.fpl_event_id
            except _InputResolutionError as exc:
                return JobResult(
                    status=JobStatus.FAILED, output=None, error_code=exc.error_code, error_message=str(exc)
                )

            try:
                started_at = datetime.now(UTC)
                run_row = ChipOptimizationRun(
                    job_id=uuid.UUID(ctx.job_id),
                    manager_squad_snapshot_id=snapshot.id,
                    season_id=season_id,
                    rules_config_id=rules_config.id,
                    run_status=ChipOptimizationRunStatus.RUNNING,
                    target_fpl_event_id=target_fpl_event_id,
                    projection_formula_version=PROJECTION_FORMULA_VERSION,
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
                captain_multiplier = get_captain_multiplier(session, constraints.rules_config_id)

                starters, bench = _split_starting_and_bench(snapshot.raw_picks_json)
                if not starters:
                    raise _SimulationBuildError(
                        "User picks' starting XI could not be read from raw_picks_json."
                    )

                element_ids = {p["element"] for p in starters + bench}
                player_by_element = _resolve_players_by_element(session, season_id, element_ids)
                positions_by_id = _positions_by_id(session, season_id)

                current_squad_starting_player_ids, current_squad_captain_player_id = (
                    _current_squad_starting_ids_and_captain(starters, player_by_element)
                )
                bank = snapshot.bank
                selling_prices = {
                    uuid.UUID(k): v for k, v in snapshot.squad_player_selling_prices_json.items()
                }

                chip_windows = resolve_chip_windows(rules_config.raw_chips_json)

                eval_ctx = _ChipEvaluationContext(
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
                )

                for chip_type in requested_chip_types:
                    windows_for_type = [w for w in chip_windows if w.chip_type == chip_type]
                    _process_chip(
                        session,
                        run_row.id,
                        chip_type,
                        windows_for_type,
                        snapshot.chips_used_json,
                        snapshot.active_chip,
                        target_fpl_event_id,
                        eval_ctx,
                    )

                run_row.run_status = ChipOptimizationRunStatus.SUCCEEDED
                run_row.completed_at = datetime.now(UTC)
                session.commit()

                return JobResult(
                    status=JobStatus.SUCCEEDED,
                    output={
                        "chip_optimization_run_id": str(run_row.id),
                        "target_fpl_event_id": target_fpl_event_id,
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
