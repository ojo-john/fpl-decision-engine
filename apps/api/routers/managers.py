"""POST /managers/sync and POST /squads/manual endpoints.

``/managers/sync`` uses the request's own DB session for the outer ``Job``
bookkeeping via ``execute_job`` — the job handler internally opens its own
session for the ``manager_squad_snapshots`` row, exactly like
``DataIngestionJobRunner``/``optimize_initial_squad`` already do (see
``app/jobs/handlers/data_ingestion.py``/``apps/api/routers/optimization.py``);
this is an established pattern, not a new one.

``/squads/manual`` is deliberately NOT routed through the job system — it's
a synchronous, validated DB write with no external I/O and nothing
retry-worthy, the same proportionality judgment already made elsewhere in
this codebase for lightweight operations.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.jobs.service import execute_job
from apps.api.dependencies import get_db
from apps.api.schemas.managers import ManagerSquadSnapshotOut, ManagerSyncRequest, ManualSquadRequest
from packages.database.models.manager_squad_snapshot import ManagerSquadSnapshot
from packages.database.models.player import Player
from packages.database.models.season import Season
from packages.domain.jobs import JobStatus, JobType, TriggerType
from packages.domain.transfers import ManagerSquadSnapshotSource

router_managers = APIRouter(prefix="/managers", tags=["managers"])
router_squads = APIRouter(prefix="/squads", tags=["squads"])


def _error_response(job) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "error_code": job.error_code,
            "error_message": job.error_message,
            "job_id": str(job.id),
        },
    )


def _current_season_id(db: Session) -> uuid.UUID:
    season_id = db.execute(select(Season.id).where(Season.is_current.is_(True))).scalar_one_or_none()
    if season_id is None:
        raise HTTPException(status_code=404, detail="No current season found.")
    return season_id


def _load_snapshot_response(db: Session, snapshot_id: uuid.UUID) -> ManagerSquadSnapshotOut:
    snapshot = db.get(ManagerSquadSnapshot, snapshot_id)
    assert snapshot is not None  # a SUCCEEDED sync / just-inserted manual entry always leaves this row
    return ManagerSquadSnapshotOut(
        manager_squad_snapshot_id=snapshot.id,
        source=snapshot.source.value,
        bank=snapshot.bank,
        free_transfers_available=snapshot.free_transfers_available,
        squad_player_ids=[uuid.UUID(pid) for pid in snapshot.squad_player_ids],
        has_unresolved_players=snapshot.has_unresolved_players,
        unresolved_fpl_element_ids=snapshot.unresolved_fpl_element_ids_json,
        created_at=snapshot.created_at,
    )


@router_managers.post("/sync", response_model=ManagerSquadSnapshotOut)
def sync_manager(
    body: ManagerSyncRequest, db: Session = Depends(get_db)
) -> ManagerSquadSnapshotOut | JSONResponse:
    job = execute_job(
        db,
        job_type=JobType.MANAGER_SYNC,
        parameters={"fpl_manager_id": body.fpl_manager_id},
        requested_by=body.requested_by,
        trigger_type=TriggerType.API,
    )
    if job.status != JobStatus.SUCCEEDED:
        return _error_response(job)

    assert job.output_json is not None  # a SUCCEEDED job always has output_json
    snapshot_id = uuid.UUID(job.output_json["manager_squad_snapshot_id"])
    return _load_snapshot_response(db, snapshot_id)


@router_squads.post("/manual", response_model=ManagerSquadSnapshotOut, status_code=201)
def create_manual_squad(body: ManualSquadRequest, db: Session = Depends(get_db)) -> ManagerSquadSnapshotOut:
    season_id = _current_season_id(db)

    players = (
        db.execute(
            select(Player).where(
                Player.season_id == season_id, Player.fpl_element_id.in_(body.fpl_element_ids)
            )
        )
        .scalars()
        .all()
    )
    player_by_element_id = {p.fpl_element_id: p for p in players}

    # Stricter than MANAGER_SYNC: an unresolvable id here is a validation
    # error, not a tolerated "unresolved player" — see ManualSquadRequest's
    # own docstring for why this asymmetry is deliberate.
    missing = [eid for eid in body.fpl_element_ids if eid not in player_by_element_id]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"fpl_element_ids not resolvable against the current season's players: {missing}",
        )

    squad_player_ids: list[str] = []
    purchase_price_by_player: dict[str, int] = {}
    selling_price_by_player: dict[str, int] = {}
    for element_id in body.fpl_element_ids:
        player = player_by_element_id[element_id]
        player_id_str = str(player.id)
        squad_player_ids.append(player_id_str)
        # No profit assumed for a manually-entered squad -- purchase price =
        # selling price = current now_cost, the same conservative fallback
        # MANAGER_SYNC uses for a player with no derivable purchase price.
        purchase_price_by_player[player_id_str] = player.now_cost
        selling_price_by_player[player_id_str] = player.now_cost

    snapshot = ManagerSquadSnapshot(
        season_id=season_id,
        source=ManagerSquadSnapshotSource.MANUAL_ENTRY,
        fpl_manager_id=None,
        job_id=None,
        as_of_gameweek_id=None,
        bank=body.bank,
        free_transfers_available=body.free_transfers_available,
        active_chip=body.active_chip,
        chips_used_json=None,
        overall_rank=None,
        total_points=None,
        squad_player_ids=squad_player_ids,
        squad_player_purchase_prices_json=purchase_price_by_player,
        squad_player_selling_prices_json=selling_price_by_player,
        estimated_purchase_price_player_ids_json=[],
        unresolved_fpl_element_ids_json=[],
        has_unresolved_players=False,
        raw_entry_json=None,
        raw_history_json=None,
        raw_picks_json=None,
        raw_transfers_json=None,
        # Never set for manual entries -- they're addressed directly by their
        # own snapshot id, not looked up "as the current one" for a manager.
        is_current=False,
        superseded_at=None,
    )
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)

    return _load_snapshot_response(db, snapshot.id)
