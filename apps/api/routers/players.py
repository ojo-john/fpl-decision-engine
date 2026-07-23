"""GET /players endpoints, scoped to the current season only."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.optimization.champion_loader import load_current_champion
from app.optimization.gameweek_resolution import resolve_target_gameweek
from app.optimization.projection import (
    PROJECTION_FORMULA_VERSION,
    project_player,
    project_player_with_champion,
)
from apps.api.dependencies import get_db
from apps.api.schemas.players import (
    PlayerDetail,
    PlayerListResponse,
    PlayerProjectionResponse,
    PlayerSummary,
)
from packages.database.models.player import Player
from packages.database.models.position import Position
from packages.database.models.season import Season
from packages.database.models.team import Team

router = APIRouter(prefix="/players", tags=["players"])

MAX_PAGE_SIZE = 200


def _current_season_id(db: Session) -> uuid.UUID:
    season_id = db.execute(select(Season.id).where(Season.is_current.is_(True))).scalar_one_or_none()
    if season_id is None:
        raise HTTPException(status_code=404, detail="No current season found.")
    return season_id


@router.get("", response_model=PlayerListResponse)
def list_players(
    position: str | None = Query(None, description="Position short name, e.g. GKP/DEF/MID/FWD"),
    team_id: uuid.UUID | None = Query(None),
    search: str | None = Query(None, description="Substring match on web_name"),
    include_quarantined: bool = Query(False),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    db: Session = Depends(get_db),
) -> PlayerListResponse:
    season_id = _current_season_id(db)

    position_id: uuid.UUID | None = None
    if position is not None:
        position_id = db.execute(
            select(Position.id).where(
                Position.season_id == season_id, Position.short_name == position.upper()
            )
        ).scalar_one_or_none()
        if position_id is None:
            # No such position for this season — a well-formed empty page, not an error.
            return PlayerListResponse(items=[], page=page, page_size=page_size, total=0)

    base_query = select(Player).where(Player.season_id == season_id)
    if not include_quarantined:
        base_query = base_query.where(Player.is_quarantined.is_(False))
    if team_id is not None:
        base_query = base_query.where(Player.team_id == team_id)
    if position_id is not None:
        base_query = base_query.where(Player.position_id == position_id)
    if search:
        base_query = base_query.where(Player.web_name.ilike(f"%{search}%"))

    total = db.execute(select(func.count()).select_from(base_query.subquery())).scalar_one()

    rows = (
        db.execute(base_query.order_by(Player.web_name).offset((page - 1) * page_size).limit(page_size))
        .scalars()
        .all()
    )

    team_ids = {p.team_id for p in rows if p.team_id is not None}
    position_ids = {p.position_id for p in rows if p.position_id is not None}
    teams_by_id = (
        {t.id: t for t in db.execute(select(Team).where(Team.id.in_(team_ids))).scalars()} if team_ids else {}
    )
    positions_by_id = (
        {p.id: p for p in db.execute(select(Position).where(Position.id.in_(position_ids))).scalars()}
        if position_ids
        else {}
    )

    items = [
        PlayerSummary(
            id=p.id,
            fpl_element_id=p.fpl_element_id,
            web_name=p.web_name,
            team_id=p.team_id,
            team_short_name=teams_by_id[p.team_id].short_name if p.team_id in teams_by_id else None,
            position=positions_by_id[p.position_id].short_name if p.position_id in positions_by_id else None,
            now_cost=p.now_cost,
            status=p.status,
            total_points=p.total_points,
            form=p.form,
            points_per_game=p.points_per_game,
            ep_next=p.ep_next,
            is_quarantined=p.is_quarantined,
        )
        for p in rows
    ]
    return PlayerListResponse(items=items, page=page, page_size=page_size, total=total)


@router.get("/{player_id}", response_model=PlayerDetail)
def get_player(player_id: uuid.UUID, db: Session = Depends(get_db)) -> PlayerDetail:
    player = db.get(Player, player_id)
    if player is None:
        raise HTTPException(status_code=404, detail=f"Player {player_id} not found.")
    return PlayerDetail.model_validate(player)


@router.get("/{player_id}/projection", response_model=PlayerProjectionResponse)
def get_player_projection(player_id: uuid.UUID, db: Session = Depends(get_db)) -> PlayerProjectionResponse:
    """Champion-aware live projection — mirrors exactly what the optimizer would actually use.

    Resolves a target gameweek scoped to the player's own season (same
    ``resolve_target_gameweek`` helper the optimizer jobs use) and, if one
    resolves and a champion model exists, uses
    ``project_player_with_champion``; falls back to the plain
    ``project_player`` formula otherwise (no active gameweek, or no
    champion yet) — identical fallback behavior to the optimizer jobs.
    """
    player = db.get(Player, player_id)
    if player is None:
        raise HTTPException(status_code=404, detail=f"Player {player_id} not found.")

    target_gameweek_id, _ = resolve_target_gameweek(db, player.season_id)
    if target_gameweek_id is None:
        projection = project_player(player)
    else:
        champion = load_current_champion(db)
        projection = project_player_with_champion(db, player, target_gameweek_id, champion)

    return PlayerProjectionResponse(
        player_id=player.id,
        projected_points=projection.projected_points,
        source=projection.source,
        is_eligible=projection.is_eligible,
        projection_formula_version=PROJECTION_FORMULA_VERSION,
        chance_of_playing_next_round=player.chance_of_playing_next_round,
        status=player.status,
    )
