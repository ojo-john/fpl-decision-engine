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
"""

import random
import uuid
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
from app.optimization.projection import project_player_with_champion
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


def _starting_xi_picks(raw_picks_json: dict | None) -> list[dict]:
    if not raw_picks_json or not raw_picks_json.get("picks"):
        return []
    return [p for p in raw_picks_json["picks"] if p.get("position", 99) <= _STARTING_XI_MAX_POSITION]


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


class _ResidualCache:
    """Per-job-run cache of empirical (actual - xP) residuals: one query per player, one per position.

    Scoped to a single job run (never persisted/shared across runs) --
    residual pools are cheap to recompute and the whole point is to reflect
    the CURRENT season's data at simulation time.
    """

    def __init__(self, session: Session, season_id: uuid.UUID):
        self._session = session
        self._season_id = season_id
        self._by_player: dict[uuid.UUID, tuple[float, ...]] = {}
        self._by_position: dict[uuid.UUID, tuple[float, ...]] = {}

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

    def for_position(self, position_id: uuid.UUID | None) -> tuple[float, ...]:
        if position_id is None:
            return ()
        if position_id not in self._by_position:
            rows = self._session.execute(
                select(PlayerGameweekStats.total_points, PlayerGameweekStats.xp_estimate)
                .join(Player, Player.id == PlayerGameweekStats.player_id)
                .where(
                    Player.season_id == self._season_id,
                    Player.position_id == position_id,
                    PlayerGameweekStats.xp_estimate.is_not(None),
                )
            ).all()
            self._by_position[position_id] = tuple(float(total) - float(xp) for total, xp in rows)
        return self._by_position[position_id]


def _build_user_players(
    session: Session,
    season_id: uuid.UUID,
    snapshot: ManagerSquadSnapshot,
    target_gameweek_id: uuid.UUID,
    champion: LoadedChampion | None,
    residuals: _ResidualCache,
) -> list[UserPlayerInput]:
    picks = _starting_xi_picks(snapshot.raw_picks_json)
    if not picks:
        raise _SimulationBuildError("User snapshot's starting XI could not be read from raw_picks_json.")

    player_by_element = _resolve_players_by_element(session, season_id, {p["element"] for p in picks})

    user_players: list[UserPlayerInput] = []
    for pick in picks:
        player = player_by_element.get(pick["element"])
        if player is None:
            continue  # unresolved element (renamed/removed player) -- degrades gracefully, see docstring
        projection = project_player_with_champion(session, player, target_gameweek_id, champion)
        user_players.append(
            UserPlayerInput(
                player_id=str(player.id),
                projected_points=projection.projected_points,
                multiplier=pick.get("multiplier", 1),
                own_residuals=residuals.for_player(player.id),
                position_residuals=residuals.for_position(player.position_id),
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
    picks = _starting_xi_picks(rival_snapshot.raw_picks_json)
    if not picks:
        raise _SimulationBuildError("Rival snapshot's starting XI could not be read from raw_picks_json.")

    player_by_element = _resolve_players_by_element(session, season_id, {p["element"] for p in picks})
    positions = _positions_by_id(session, season_id)

    held: list[RivalHeldPlayerInput] = []
    for pick in picks:
        player = player_by_element.get(pick["element"])
        if player is None:
            continue  # unresolved element -- degrades gracefully, mirrors the user-side handling
        projection = project_player_with_champion(session, player, target_gameweek_id, champion)
        held.append(
            RivalHeldPlayerInput(
                player_id=str(player.id),
                fpl_element_id=pick["element"],
                position=positions.get(player.position_id) if player.position_id is not None else None,
                projected_points=projection.projected_points,
                own_residuals=residuals.for_player(player.id),
                position_residuals=residuals.for_position(player.position_id),
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
                position_residuals=residuals.for_position(player.position_id),
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
