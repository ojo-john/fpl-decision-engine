"""TRANSFER_OPTIMIZATION job: evaluate 0/1/2-transfer scenarios for an existing manager squad.

Milestone 4 Layer 3 — wires Layer 1 (manager squad snapshot: free transfers,
bank, selling prices) and Layer 2 (candidate pool construction, horizon
projection, scenario search) into a persisted job handler.

Follows ``SquadOptimizationJobRunner``'s create-early-RUNNING-row discipline
(not ``ManagerSyncJobRunner``'s build-fully-in-memory-first one): a
``transfer_optimization_runs`` row is created immediately after resolving
inputs (snapshot, gate, constraints, transfer rules, horizon), before the
actual candidate-building/scenario-search work, so a crash mid-search still
leaves a terminal (FAILED) row behind via ``_finalize_failed`` rather than no
trace at all.

Unresolved-squad-player handling: ``ManagerSquadSnapshot.squad_player_ids``
already excludes any FPL element that didn't resolve to a current-season
``Player`` row (see ``app/jobs/handlers/manager_sync.py``) — so this handler
naturally only ever builds ``TransferCandidate`` OUT entries for the resolved
subset. A squad with ``has_unresolved_players=True`` simply has fewer than 15
OUT candidates and no ability to select an unresolved player as an OUT move;
this degrades correctly with no special-casing needed here.

Unlike ``SQUAD_OPTIMIZATION``/``LINEUP_OPTIMIZATION``, an empty resolved
horizon (``resolve_horizon_gameweeks`` returns ``[]`` — the real off-season
state) is treated as a hard refusal here (no row created), not a "fall back
to the plain formula and proceed" case: there is no meaningful multi-
gameweek transfer plan to evaluate at all with zero target gameweeks, unlike
squad/lineup selection, which still produces a useful recommendation for the
*next* squad even without a live gameweek to score it against.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.jobs.registry import register_handler
from app.jobs.runner import JobContext, JobResult, JobRunner
from app.optimization.champion_loader import LoadedChampion, load_current_champion
from app.optimization.constraints import load_optimization_constraints
from app.optimization.gameweek_resolution import resolve_horizon_gameweeks
from app.optimization.gate import check_optimization_gate
from app.optimization.projection import PROJECTION_FORMULA_VERSION
from app.transfers.candidates import (
    SquadPlayerInfo,
    TransferCandidate,
    build_in_shortlists,
    build_out_candidates,
)
from app.transfers.horizon_projection import compute_horizon_points
from app.transfers.rules import RulesBlockedError, load_transfer_rules
from app.transfers.scenario_search import TransferScenario, evaluate_transfer_scenarios
from packages.database.models.manager_squad_snapshot import ManagerSquadSnapshot
from packages.database.models.player import Player
from packages.database.models.position import Position
from packages.database.models.transfer_optimization_run import TransferOptimizationRun
from packages.database.models.transfer_optimization_run_move import TransferOptimizationRunMove
from packages.database.models.transfer_optimization_run_scenario import TransferOptimizationRunScenario
from packages.database.session import SessionLocal
from packages.domain.jobs import JobStatus, JobType
from packages.domain.transfers import TransferOptimizationRunStatus

DEFAULT_HORIZON_GAMEWEEKS = 6
MIN_HORIZON_GAMEWEEKS = 4
MAX_HORIZON_GAMEWEEKS = 8


def _resolve_horizon_length(parameters: dict | None) -> int:
    raw = (parameters or {}).get("horizon_gameweeks", DEFAULT_HORIZON_GAMEWEEKS)
    return max(MIN_HORIZON_GAMEWEEKS, min(MAX_HORIZON_GAMEWEEKS, int(raw)))


def _build_squad_candidates(
    session: Session,
    season_id: uuid.UUID,
    squad_player_ids: list[uuid.UUID],
    horizon_gameweek_ids: list[uuid.UUID],
    champion: LoadedChampion | None,
) -> list[TransferCandidate]:
    """Build one OUT candidate per resolved squad-member player id.

    Mirrors ``app.jobs.handlers.squad_optimization._build_candidates``'s
    Player/Position join, but scoped to exactly the snapshot's squad member
    ids rather than the whole season's player pool, and with no eligibility
    filter (see ``TransferCandidate.is_eligible``'s own docstring: an OUT
    candidate is always eligible regardless of current availability status).
    """
    players = (
        session.execute(select(Player).where(Player.season_id == season_id, Player.id.in_(squad_player_ids)))
        .scalars()
        .all()
    )
    positions_by_id = {
        p.id: p for p in session.execute(select(Position).where(Position.season_id == season_id)).scalars()
    }

    players_by_id: dict[uuid.UUID, SquadPlayerInfo] = {}
    horizon_points_by_id: dict[uuid.UUID, float] = {}
    for player in players:
        if player.team_id is None or player.position_id is None:
            continue
        position = positions_by_id.get(player.position_id)
        if position is None:
            continue
        players_by_id[player.id] = SquadPlayerInfo(
            position_short_name=position.short_name, team_id=player.team_id, now_cost=player.now_cost
        )
        horizon_points_by_id[player.id] = compute_horizon_points(
            session, player, horizon_gameweek_ids, champion
        )

    # Preserve the snapshot's own ordering, filtered to the resolvable subset.
    resolved_ids = [pid for pid in squad_player_ids if pid in players_by_id]
    return build_out_candidates(resolved_ids, players_by_id, horizon_points_by_id)


def _persist_scenarios_and_moves(
    session: Session,
    run_id: uuid.UUID,
    scenarios: dict[int, TransferScenario],
    recommended: TransferScenario,
    selling_prices: dict[uuid.UUID, int],
) -> None:
    """Persist all 3 scenario rows and their moves, flagging exactly one as recommended."""
    scenario_rows: dict[int, TransferOptimizationRunScenario] = {}
    for count, scenario in scenarios.items():
        scenario_row = TransferOptimizationRunScenario(
            transfer_optimization_run_id=run_id,
            transfers_count=scenario.transfers_count,
            raw_horizon_points_gain=Decimal(str(scenario.raw_horizon_points_gain)),
            hit_cost=scenario.hit_cost,
            net_horizon_points_gain=Decimal(str(scenario.net_horizon_points_gain)),
            is_recommended=(scenario.transfers_count == recommended.transfers_count),
        )
        session.add(scenario_row)
        scenario_rows[count] = scenario_row

    # Flush (not commit) so scenario_row.id is populated for the move rows'
    # FK references, without ending the outer transaction early.
    session.flush()

    for count, scenario in scenarios.items():
        scenario_row = scenario_rows[count]
        for index, move in enumerate(scenario.moves):
            session.add(
                TransferOptimizationRunMove(
                    transfer_optimization_run_scenario_id=scenario_row.id,
                    move_index=index,
                    player_out_id=move.player_out.player_id,
                    player_out_selling_price_snapshot=selling_prices[move.player_out.player_id],
                    player_in_id=move.player_in.player_id,
                    player_in_cost_snapshot=move.player_in.now_cost,
                    projected_points_gain_over_horizon=Decimal(str(move.points_gain)),
                )
            )


@register_handler(JobType.TRANSFER_OPTIMIZATION)
class TransferOptimizationJobRunner(JobRunner):
    """Evaluates ROLL/1-transfer/2-transfer scenarios for a manager's current squad."""

    def run(self, ctx: JobContext) -> JobResult:
        session = SessionLocal()
        run_row: TransferOptimizationRun | None = None
        try:
            raw_snapshot_id = (ctx.parameters or {}).get("manager_squad_snapshot_id")
            if raw_snapshot_id is None:
                return JobResult(
                    status=JobStatus.FAILED,
                    output=None,
                    error_code="MISSING_SNAPSHOT_ID",
                    error_message="parameters.manager_squad_snapshot_id is required.",
                )
            try:
                manager_squad_snapshot_id = uuid.UUID(str(raw_snapshot_id))
            except (ValueError, AttributeError, TypeError):
                return JobResult(
                    status=JobStatus.FAILED,
                    output=None,
                    error_code="INVALID_SNAPSHOT_ID",
                    error_message="parameters.manager_squad_snapshot_id is not a valid UUID.",
                )

            horizon_length = _resolve_horizon_length(ctx.parameters)

            snapshot = session.get(ManagerSquadSnapshot, manager_squad_snapshot_id)
            if snapshot is None:
                return JobResult(
                    status=JobStatus.FAILED,
                    output=None,
                    error_code="SNAPSHOT_NOT_FOUND",
                    error_message=f"manager_squad_snapshots row {manager_squad_snapshot_id} not found.",
                )
            season_id = snapshot.season_id

            gate = check_optimization_gate(session, season_id)
            if not gate.allowed:
                return JobResult(
                    status=JobStatus.FAILED,
                    output=None,
                    error_code=gate.reason_code,
                    error_message=f"Optimization gate refused: {gate.reason_code}",
                )

            try:
                started_at = datetime.now(UTC)
                constraints = load_optimization_constraints(session, season_id)
                transfer_rules = load_transfer_rules(session, constraints.rules_config_id)
                horizon_gameweek_ids = resolve_horizon_gameweeks(session, season_id, horizon_length)
                if not horizon_gameweek_ids:
                    return JobResult(
                        status=JobStatus.FAILED,
                        output=None,
                        error_code="NO_ACTIVE_GAMEWEEK",
                        error_message="No unfinished gameweek found to build a transfer horizon against.",
                    )

                transfer_hit_cost_magnitude = abs(transfer_rules.transfer_hit_cost)

                run_row = TransferOptimizationRun(
                    job_id=uuid.UUID(ctx.job_id),
                    manager_squad_snapshot_id=snapshot.id,
                    season_id=season_id,
                    rules_config_id=constraints.rules_config_id,
                    run_status=TransferOptimizationRunStatus.RUNNING,
                    horizon_start_gameweek_id=horizon_gameweek_ids[0],
                    horizon_length=horizon_length,
                    horizon_gameweek_ids_json=[str(gw_id) for gw_id in horizon_gameweek_ids],
                    free_transfers_available_snapshot=snapshot.free_transfers_available,
                    bank_snapshot=snapshot.bank,
                    transfer_hit_cost_snapshot=transfer_hit_cost_magnitude,
                    optimization_permitted_snapshot=gate.optimization_permitted,
                    data_quality_score_snapshot=(
                        Decimal(str(gate.data_quality_score)) if gate.data_quality_score is not None else None
                    ),
                    rules_status_snapshot=gate.rules_status,
                    projection_formula_version=PROJECTION_FORMULA_VERSION,
                    parameters_json=ctx.parameters,
                    started_at=started_at,
                )
                session.add(run_row)
                session.commit()
                session.refresh(run_row)

                # Loaded ONCE per job run, never per-player/per-candidate.
                champion = load_current_champion(session)

                squad_player_ids = [uuid.UUID(pid) for pid in snapshot.squad_player_ids]
                squad_candidates = _build_squad_candidates(
                    session, season_id, squad_player_ids, horizon_gameweek_ids, champion
                )

                in_shortlists = build_in_shortlists(
                    session,
                    season_id,
                    excluded_player_ids=set(squad_player_ids),
                    horizon_gameweek_ids=horizon_gameweek_ids,
                    champion=champion,
                )

                # Selling prices come from the SNAPSHOT (Layer 1's own
                # derivation), never recomputed here.
                selling_prices = {
                    uuid.UUID(k): v for k, v in snapshot.squad_player_selling_prices_json.items()
                }

                scenarios, recommended = evaluate_transfer_scenarios(
                    squad_candidates,
                    in_shortlists,
                    selling_prices,
                    constraints.team_limit,
                    snapshot.bank,
                    snapshot.free_transfers_available,
                    transfer_hit_cost_magnitude,
                )

                _persist_scenarios_and_moves(session, run_row.id, scenarios, recommended, selling_prices)

                run_row.run_status = TransferOptimizationRunStatus.SUCCEEDED
                run_row.recommended_transfers_count = recommended.transfers_count
                run_row.recommended_hit_cost = recommended.hit_cost
                run_row.recommended_net_horizon_points_gain = Decimal(
                    str(recommended.net_horizon_points_gain)
                )
                run_row.completed_at = datetime.now(UTC)
                session.commit()

                return JobResult(
                    status=JobStatus.SUCCEEDED,
                    output={
                        "transfer_optimization_run_id": str(run_row.id),
                        "recommended_transfers_count": recommended.transfers_count,
                        "recommended_hit_cost": recommended.hit_cost,
                        "recommended_net_horizon_points_gain": float(recommended.net_horizon_points_gain),
                    },
                    error_code=None,
                    error_message=None,
                )
            except RulesBlockedError as exc:
                # Always raised before run_row is created (constraints/transfer
                # rules are loaded up front) -- no row to finalize, mirrors
                # ManagerSyncJobRunner's own RulesBlockedError handling.
                session.rollback()
                return JobResult(
                    status=JobStatus.FAILED,
                    output=None,
                    error_code="RULES_BLOCKED",
                    error_message=str(exc),
                )
            except Exception as exc:  # noqa: BLE001 - the run row must always reach a terminal status
                session.rollback()
                self._finalize_failed(session, run_row)
                return JobResult(
                    status=JobStatus.FAILED,
                    output={"transfer_optimization_run_id": str(run_row.id)} if run_row is not None else None,
                    error_code="TRANSFER_OPTIMIZATION_FAILED",
                    error_message=str(exc),
                )
        finally:
            session.close()

    def _finalize_failed(self, session: Session, run_row: TransferOptimizationRun | None) -> None:
        if run_row is None or run_row.id is None:
            return
        try:
            fresh = session.get(TransferOptimizationRun, run_row.id)
            if fresh is not None and fresh.run_status == TransferOptimizationRunStatus.RUNNING:
                fresh.run_status = TransferOptimizationRunStatus.FAILED
                fresh.completed_at = datetime.now(UTC)
                session.commit()
        except Exception:  # noqa: BLE001 - best-effort finalization only
            session.rollback()
