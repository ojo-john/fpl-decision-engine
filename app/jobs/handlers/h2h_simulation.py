"""H2H_SIMULATION job: Monte Carlo-simulate one user-vs-rival matchup for a target gameweek.

Milestone 5 Layer 3 — wires Layer 1 (``ManagerSquadSnapshot``: the user's own
last-known squad), Layer 2 (``RivalProfile``: the rival's derived/predicted
state), and Milestone 3 Layer 4's ``project_player_with_champion`` together
into ``app.h2h.simulate.simulate_matchup``, then persists the result.

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
``is_captain`` for the user's deterministic captain choice). A snapshot with
no ``raw_picks_json`` (e.g. a hand-built ``MANUAL_ENTRY`` squad with no picks
recorded) cannot be simulated at all in this v1 — refused up front with
``SNAPSHOT_MISSING_PICKS_DATA``/``RIVAL_SQUAD_MISSING_PICKS_DATA``, a known,
documented limitation rather than a silent guess at an ordering.

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

Residual sourcing: ``_ResidualCache`` prefers a player's own current-season
``player_gameweek_stats`` history; when that's empty (the realistic
early-season/fresh-database state — no gameweeks of the CURRENT season have
been played/ingested yet), it falls back to a same-position pool drawn
across EVERY ingested season (matched by ``Position.short_name``, since
``Position``/``Player`` rows are season-scoped and a position's own id
differs season to season) rather than a current-season-only pool that would
otherwise be silently empty and force every projection down to zero-variance
noise until the current season's own history accumulates.
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
    simulate_matchup,
)
from app.jobs.registry import register_handler
from app.jobs.runner import JobContext, JobResult, JobRunner
from app.optimization.champion_loader import LoadedChampion, load_current_champion
from app.optimization.gameweek_resolution import (
    resolve_most_recent_finished_gameweek_event,
    resolve_target_gameweek,
)
from app.optimization.gate import check_optimization_gate
from app.optimization.projection import PlayerProjection, project_player_with_champion
from packages.database.models.gameweek import Gameweek
from packages.database.models.h2h_simulation_run import H2HSimulationRun
from packages.database.models.manager_squad_snapshot import ManagerSquadSnapshot
from packages.database.models.player import Player
from packages.database.models.player_gameweek_stats import PlayerGameweekStats
from packages.database.models.position import Position
from packages.database.models.rival_gameweek_snapshot import RivalGameweekSnapshot
from packages.database.models.rival_profile import RivalProfile
from packages.database.session import SessionLocal
from packages.domain.h2h import H2HSimulationRunStatus
from packages.domain.jobs import JobStatus, JobType
from packages.domain.optimization import TargetGameweekStatus

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


class _ResidualCache:
    """Per-job-run cache of empirical (actual - xP) residuals: one query per player, one per position.

    Scoped to a single job run (never persisted/shared across runs) --
    residual pools are cheap to recompute and the whole point is to reflect
    the latest data at simulation time.

    ``for_position`` prefers a CURRENT-season, same-position pool, but falls
    back to an ALL-seasons, same-position pool (matched by
    ``Position.short_name``, since ``Position``/``Player`` rows -- and
    therefore ``position_id`` -- are season-scoped) when the current
    season's own pool is empty. This matters concretely at the start of a
    fresh season (the realistic state for a brand-new current season with no
    gameweeks played/ingested yet): without this fallback, EVERY player's
    residual sampling would silently degrade to zero noise (see
    ``app.h2h.simulate._sample_residual``'s final fallback) simply because
    the current season hasn't accumulated its own history yet, not because
    no relevant empirical data exists anywhere in the system.
    """

    def __init__(self, session: Session, season_id: uuid.UUID):
        self._session = session
        self._season_id = season_id
        self._by_player: dict[uuid.UUID, tuple[float, ...]] = {}
        self._by_position_current_season: dict[uuid.UUID, tuple[float, ...]] = {}
        self._by_position_all_seasons: dict[str, tuple[float, ...]] = {}

    def for_player(self, player_id: uuid.UUID) -> tuple[float, ...]:
        if player_id not in self._by_player:
            rows = self._session.execute(
                select(PlayerGameweekStats.total_points, PlayerGameweekStats.xp_estimate).where(
                    PlayerGameweekStats.player_id == player_id,
                    PlayerGameweekStats.xp_estimate.is_not(None),
                )
            ).all()
            self._by_player[player_id] = tuple(float(total) - float(xp) for total, xp in rows)
        return self._by_player[player_id]

    def for_position(
        self, position_id: uuid.UUID | None, position_short_name: str | None
    ) -> tuple[float, ...]:
        current_season_pool = self._current_season_pool(position_id) if position_id is not None else ()
        if current_season_pool:
            return current_season_pool
        if position_short_name is not None:
            return self._all_seasons_pool(position_short_name)
        return ()

    def _current_season_pool(self, position_id: uuid.UUID) -> tuple[float, ...]:
        if position_id not in self._by_position_current_season:
            rows = self._session.execute(
                select(PlayerGameweekStats.total_points, PlayerGameweekStats.xp_estimate)
                .join(Player, Player.id == PlayerGameweekStats.player_id)
                .where(
                    Player.season_id == self._season_id,
                    Player.position_id == position_id,
                    PlayerGameweekStats.xp_estimate.is_not(None),
                )
            ).all()
            self._by_position_current_season[position_id] = tuple(
                float(total) - float(xp) for total, xp in rows
            )
        return self._by_position_current_season[position_id]

    def _all_seasons_pool(self, position_short_name: str) -> tuple[float, ...]:
        if position_short_name not in self._by_position_all_seasons:
            rows = self._session.execute(
                select(PlayerGameweekStats.total_points, PlayerGameweekStats.xp_estimate)
                .join(Player, Player.id == PlayerGameweekStats.player_id)
                .join(Position, Position.id == Player.position_id)
                .where(
                    Position.short_name == position_short_name,
                    PlayerGameweekStats.xp_estimate.is_not(None),
                )
            ).all()
            self._by_position_all_seasons[position_short_name] = tuple(
                float(total) - float(xp) for total, xp in rows
            )
        return self._by_position_all_seasons[position_short_name]


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


def _build_user_players(
    session: Session,
    season_id: uuid.UUID,
    snapshot: ManagerSquadSnapshot,
    target_gameweek_id: uuid.UUID,
    champion: LoadedChampion | None,
    residuals: _ResidualCache,
) -> list[UserPlayerInput]:
    starters, bench = _split_starting_and_bench(snapshot.raw_picks_json)
    if not starters:
        raise _SimulationBuildError("User snapshot's starting XI could not be read from raw_picks_json.")

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
    return user_players


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

                if not snapshot.raw_picks_json or not snapshot.raw_picks_json.get("picks"):
                    raise _InputResolutionError(
                        "SNAPSHOT_MISSING_PICKS_DATA",
                        "manager_squad_snapshot has no raw_picks_json to resolve a starting XI from.",
                    )

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

                user_players = _build_user_players(
                    session, season_id, snapshot, target_gameweek_id, champion, residuals
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

                inputs = SimulationInputs(
                    user_players=tuple(user_players),
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
                result = simulate_matchup(inputs, random.Random())

                run_row.sample_count = result.sample_count
                run_row.user_score_mean = Decimal(str(round(result.user_score_mean, 2)))
                run_row.user_score_floor = Decimal(str(round(result.user_score_floor, 2)))
                run_row.user_score_ceiling = Decimal(str(round(result.user_score_ceiling, 2)))
                run_row.rival_score_mean = Decimal(str(round(result.rival_score_mean, 2)))
                run_row.rival_score_floor = Decimal(str(round(result.rival_score_floor, 2)))
                run_row.rival_score_ceiling = Decimal(str(round(result.rival_score_ceiling, 2)))
                run_row.win_probability = Decimal(str(round(result.win_probability, 4)))
                run_row.draw_probability = Decimal(str(round(result.draw_probability, 4)))
                run_row.loss_probability = Decimal(str(round(result.loss_probability, 4)))
                run_row.expected_margin = Decimal(str(round(result.expected_margin, 2)))
                run_row.margin_std_dev = Decimal(str(round(result.margin_std_dev, 2)))
                run_row.confidence = result.confidence
                run_row.run_status = H2HSimulationRunStatus.SUCCEEDED
                run_row.completed_at = datetime.now(UTC)
                session.commit()

                return JobResult(
                    status=JobStatus.SUCCEEDED,
                    output={
                        "h2h_simulation_run_id": str(run_row.id),
                        "win_probability": float(result.win_probability),
                        "draw_probability": float(result.draw_probability),
                        "loss_probability": float(result.loss_probability),
                        "expected_margin": float(result.expected_margin),
                        "confidence": result.confidence.value,
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
