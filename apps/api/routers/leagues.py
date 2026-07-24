"""``/leagues`` endpoints: trigger and read LEAGUE_SYNC output (Milestone 8 Layers 1 & 3).

LEAGUE_SYNC (Milestone 5 Layer 1/2) previously had zero REST exposure --
reachable only via ``scripts/run_job.py`` or the scheduled dispatcher.
``POST /leagues/sync`` mirrors every other job-triggering endpoint's exact
shape (``execute_job`` -> 409-with-``error_code`` on non-success -> assembled
response on success -- see ``apps.api.routers.recommendations``). The GET
endpoints below it are plain, capped-result reads over already-persisted
rows -- no job execution involved.
"""

import uuid
from collections.abc import Sequence

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.h2h.differential import SquadDifferential, compute_squad_differential
from app.jobs.service import execute_job
from apps.api.dependencies import get_db
from apps.api.schemas.leagues import (
    LeagueEntryOut,
    LeagueOut,
    LeagueSyncRequest,
    LeagueSyncRunOut,
    RivalProfileOut,
    SquadDifferentialPlayerOut,
)
from packages.database.models.league import League
from packages.database.models.league_entry import LeagueEntry
from packages.database.models.league_sync_run import LeagueSyncRun
from packages.database.models.manager_squad_snapshot import ManagerSquadSnapshot
from packages.database.models.player import Player
from packages.database.models.position import Position
from packages.database.models.rival_gameweek_snapshot import RivalGameweekSnapshot
from packages.database.models.rival_profile import RivalProfile
from packages.database.models.season import Season
from packages.domain.jobs import JobStatus, JobType, TriggerType

router = APIRouter(prefix="/leagues", tags=["leagues"])


def _error_response(job) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "error_code": job.error_code,
            "error_message": job.error_message,
            "job_id": str(job.id),
        },
    )

# Same "capped read, not full pagination" convention as apps.api.routers.learning --
# a real H2H league's rival/standings row count never approaches a size where
# offset/limit paging is actually needed.
_MAX_RESULTS = 200


def _league_not_found_response(league_id: uuid.UUID) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={"error_code": "LEAGUE_NOT_FOUND", "detail": f"leagues row {league_id} not found."},
    )


def _current_squad_player_ids(db: Session) -> list[uuid.UUID] | None:
    """The calling manager's own current squad, for the differential computation below --
    None (not empty) when no MANAGER_SYNC squad has ever been synced, so callers can tell
    "no differential computed" apart from "computed, and the squads happen to be identical".
    Manual-entry snapshots are never ``is_current`` (see ManagerSquadSnapshot's own
    docstring), so this only ever resolves a real synced squad, matching the same squad the
    Rivals page's own H2H-simulate button already treats as "the" current squad."""
    snapshot = db.execute(
        select(ManagerSquadSnapshot).where(ManagerSquadSnapshot.is_current.is_(True)).limit(1)
    ).scalar_one_or_none()
    if snapshot is None:
        return None
    return [uuid.UUID(pid) for pid in snapshot.squad_player_ids]


def _rival_squad_player_ids_by_snapshot(
    db: Session, rows: Sequence[RivalProfile]
) -> dict[uuid.UUID, list[uuid.UUID]]:
    """Batched lookup (one query, not N+1) of each rival's last AVAILABLE picks snapshot,
    keyed by RivalProfile.last_known_snapshot_id -- see that column's own docstring."""
    snapshot_ids = [row.last_known_snapshot_id for row in rows if row.last_known_snapshot_id is not None]
    if not snapshot_ids:
        return {}
    snapshot_rows = (
        db.execute(select(RivalGameweekSnapshot).where(RivalGameweekSnapshot.id.in_(snapshot_ids)))
        .scalars()
        .all()
    )
    return {
        snap.id: [uuid.UUID(pid) for pid in snap.squad_player_ids]
        for snap in snapshot_rows
        if snap.squad_player_ids
    }


def _resolve_player_lookup(
    db: Session, player_ids: set[uuid.UUID]
) -> dict[uuid.UUID, SquadDifferentialPlayerOut]:
    """Batched name/position resolution (one query for every differential across every rival
    in the response, not one query per rival) -- see list_league_rivals's own call site."""
    if not player_ids:
        return {}
    rows = (
        db.execute(
            select(Player, Position)
            .outerjoin(Position, Player.position_id == Position.id)
            .where(Player.id.in_(player_ids))
        )
        .all()
    )
    return {
        player.id: SquadDifferentialPlayerOut(
            player_id=player.id,
            web_name=player.web_name,
            position=position.short_name if position is not None else None,
        )
        for player, position in rows
    }


def _sorted_differential_players(
    ids: frozenset[uuid.UUID], lookup: dict[uuid.UUID, SquadDifferentialPlayerOut]
) -> list[SquadDifferentialPlayerOut]:
    resolved = [lookup[pid] for pid in ids if pid in lookup]
    return sorted(resolved, key=lambda p: p.web_name or "")


def _rival_profile_out(
    row: RivalProfile,
    differential: SquadDifferential | None,
    player_lookup: dict[uuid.UUID, SquadDifferentialPlayerOut],
) -> RivalProfileOut:
    players_only_you_have = None
    players_only_rival_has = None
    if differential is not None:
        players_only_you_have = _sorted_differential_players(differential.only_first, player_lookup)
        players_only_rival_has = _sorted_differential_players(differential.only_second, player_lookup)
    return RivalProfileOut(
        rival_profile_id=row.id,
        fpl_manager_id=row.fpl_manager_id,
        last_known_snapshot_id=row.last_known_snapshot_id,
        derived_free_transfers_min=row.derived_free_transfers_min,
        derived_free_transfers_max=row.derived_free_transfers_max,
        derived_free_transfers_confidence=(
            row.derived_free_transfers_confidence.value
            if row.derived_free_transfers_confidence is not None
            else None
        ),
        derived_likely_remaining_chips=list(row.derived_likely_remaining_chips_json or []),
        predicted_no_transfer_probability=row.predicted_no_transfer_probability,
        predicted_one_transfer_probability=row.predicted_one_transfer_probability,
        predicted_multi_transfer_probability=row.predicted_multi_transfer_probability,
        predicted_hit_probability=row.predicted_hit_probability,
        predicted_captain_probabilities=dict(row.predicted_captain_probabilities_json or {}),
        predicted_likely_transfer_targets=list(row.predicted_likely_transfer_targets_json or []),
        tendency_features=dict(row.tendency_features_json or {}),
        unknowns=list(row.unknowns_json or []),
        evidence=dict(row.evidence_json or {}),
        overall_confidence=row.overall_confidence.value,
        computed_at=row.computed_at,
        players_only_you_have=players_only_you_have,
        players_only_rival_has=players_only_rival_has,
    )


def _league_out(row: League) -> LeagueOut:
    return LeagueOut(
        league_id=row.id, fpl_league_id=row.fpl_league_id, name=row.name, is_priority=row.is_priority
    )


def _league_entry_out(row: LeagueEntry) -> LeagueEntryOut:
    return LeagueEntryOut(
        fpl_manager_id=row.fpl_manager_id,
        player_name=row.player_name,
        entry_name=row.entry_name,
        rank=row.rank,
        last_rank=row.last_rank,
        matches_played=row.matches_played,
        matches_won=row.matches_won,
        matches_drawn=row.matches_drawn,
        matches_lost=row.matches_lost,
        points_for=row.points_for,
        total=row.total,
    )


@router.post("/sync", response_model=LeagueSyncRunOut)
def sync_league(body: LeagueSyncRequest, db: Session = Depends(get_db)) -> LeagueSyncRunOut | JSONResponse:
    parameters: dict[str, int] = {"fpl_league_id": body.fpl_league_id}
    if body.exclude_fpl_manager_id is not None:
        parameters["exclude_fpl_manager_id"] = body.exclude_fpl_manager_id

    job = execute_job(
        db,
        job_type=JobType.LEAGUE_SYNC,
        parameters=parameters,
        requested_by=body.requested_by,
        trigger_type=TriggerType.API,
    )
    if job.status != JobStatus.SUCCEEDED:
        return _error_response(job)

    assert job.output_json is not None  # a SUCCEEDED job always has output_json
    run_id = uuid.UUID(job.output_json["league_sync_run_id"])
    run = db.get(LeagueSyncRun, run_id)
    assert run is not None  # a SUCCEEDED job always leaves its league_sync_runs row behind
    return LeagueSyncRunOut(
        league_sync_run_id=run.id,
        league_id=run.league_id,
        run_status=run.run_status.value,
        rivals_synced_count=run.rivals_synced_count,
        rivals_unavailable_count=run.rivals_unavailable_count,
        started_at=run.started_at,
        completed_at=run.completed_at,
    )


@router.get("", response_model=list[LeagueOut])
def list_leagues(db: Session = Depends(get_db)) -> list[LeagueOut]:
    """List every synced league -- lets a caller resolve a league_id before calling
    the two id-keyed endpoints below (a real H2H league row persists across seasons,
    upsert-in-place, so this is always a small, capped result set)."""
    # Ordered by (name, fpl_league_id) -- name alone is not a unique key (two
    # real leagues can share a name), and an unordered/single-key-with-ties
    # query has no obligation to return a stable order across executions
    # (same bug class as app.optimization.fixture_calendar's own fix).
    # fpl_league_id (unique) breaks every tie deterministically.
    rows = (
        db.execute(select(League).order_by(League.name, League.fpl_league_id).limit(_MAX_RESULTS))
        .scalars()
        .all()
    )
    return [_league_out(row) for row in rows]


@router.post("/{league_id}/priority", response_model=LeagueOut)
def set_priority_league(league_id: uuid.UUID, db: Session = Depends(get_db)) -> LeagueOut | JSONResponse:
    """Mark this league as priority (the one Rivals shows by default) -- unsets every other
    league's flag first, same "exactly one at a time" invariant as ``leagues.is_priority``'s
    own docstring on the model."""
    league = db.get(League, league_id)
    if league is None:
        return _league_not_found_response(league_id)

    db.execute(update(League).where(League.is_priority.is_(True)).values(is_priority=False))
    league.is_priority = True
    db.commit()
    db.refresh(league)
    return _league_out(league)


@router.get("/{league_id}/rivals", response_model=list[RivalProfileOut])
def list_league_rivals(
    league_id: uuid.UUID, db: Session = Depends(get_db)
) -> list[RivalProfileOut] | JSONResponse:
    """List the CURRENT season's rival profiles last computed for this league.

    Filters on BOTH ``RivalProfile.league_id`` (provenance only -- see that
    model's own docstring: a rival's derived state is keyed on
    ``(season_id, fpl_manager_id)``, not ``league_id``) AND the current
    ``Season`` (same "scope to the current season" convention as
    ``apps.api.routers.recommendations.get_latest_recommendation``/
    ``apps.api.routers.learning.get_latest_learning_report``). ``league_id``
    alone is not enough: a real ``League`` row persists across seasons
    (upsert-in-place), so a rival synced last season and never synced again
    this season would otherwise still show up here.

    Also computes, per rival, the squad differential against the calling
    manager's own current squad (``players_only_you_have``/
    ``players_only_rival_has`` on ``RivalProfileOut`` -- see
    ``app.h2h.differential``). Batched into 3 extra queries total regardless
    of rival count (current squad, rival snapshots, player name/position
    lookup) -- never one query per rival.
    """
    if db.get(League, league_id) is None:
        return _league_not_found_response(league_id)

    rows = (
        db.execute(
            select(RivalProfile)
            .join(Season, Season.id == RivalProfile.season_id)
            .where(RivalProfile.league_id == league_id, Season.is_current.is_(True))
            .order_by(RivalProfile.fpl_manager_id)
            .limit(_MAX_RESULTS)
        )
        .scalars()
        .all()
    )

    current_squad_player_ids = _current_squad_player_ids(db)
    rival_squad_by_snapshot = _rival_squad_player_ids_by_snapshot(db, rows)

    differentials: list[SquadDifferential | None] = []
    all_differential_ids: set[uuid.UUID] = set()
    for row in rows:
        rival_squad_player_ids = (
            rival_squad_by_snapshot.get(row.last_known_snapshot_id)
            if row.last_known_snapshot_id is not None
            else None
        )
        if current_squad_player_ids is None or rival_squad_player_ids is None:
            differentials.append(None)
            continue
        differential = compute_squad_differential(current_squad_player_ids, rival_squad_player_ids)
        differentials.append(differential)
        all_differential_ids |= differential.only_first | differential.only_second

    player_lookup = _resolve_player_lookup(db, all_differential_ids)

    return [
        _rival_profile_out(row, differential, player_lookup)
        for row, differential in zip(rows, differentials, strict=True)
    ]


@router.get("/{league_id}/standings", response_model=list[LeagueEntryOut])
def list_league_standings(
    league_id: uuid.UUID, db: Session = Depends(get_db)
) -> list[LeagueEntryOut] | JSONResponse:
    if db.get(League, league_id) is None:
        return _league_not_found_response(league_id)

    rows = (
        db.execute(
            select(LeagueEntry).where(LeagueEntry.league_id == league_id).order_by(LeagueEntry.rank)
        )
        .scalars()
        .all()
    )
    return [_league_entry_out(row) for row in rows]
