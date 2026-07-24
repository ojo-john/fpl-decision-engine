"""GET /players endpoints, scoped to the current season only."""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.features.service import compute_features_for_player_gameweek
from app.optimization.champion_loader import load_current_champion
from app.optimization.gameweek_resolution import resolve_target_gameweek
from app.optimization.projection import (
    PROJECTION_FORMULA_VERSION,
    PlayerProjection,
    is_cold_start_season,
    project_player,
    project_player_with_champion,
)
from app.players.price_signal import classify_price_momentum
from apps.api.dependencies import get_db
from apps.api.schemas.players import (
    PlayerDetail,
    PlayerListResponse,
    PlayerPriceChangeOut,
    PlayerPriceMomentumOut,
    PlayerProjectionResponse,
    PlayerSummary,
)
from packages.database.models.player import Player
from packages.database.models.player_price_history import PlayerPriceHistory
from packages.database.models.position import Position
from packages.database.models.season import Season
from packages.database.models.team import Team

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/players", tags=["players"])

MAX_PAGE_SIZE = 200

# Same "capped read, not full pagination" convention as apps.api.routers.managers/optimization --
# a single player's own real price-change history never approaches a size where offset/limit
# paging is actually needed.
_MAX_PRICE_HISTORY_RESULTS = 200


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

    detail = PlayerDetail.model_validate(player)
    # Same enrichment list_players already does for PlayerSummary -- a bare team_id/position_id
    # forces every caller to look the name up separately for something this common.
    if player.team_id is not None:
        team = db.get(Team, player.team_id)
        detail.team_short_name = team.short_name if team is not None else None
    if player.position_id is not None:
        position = db.get(Position, player.position_id)
        detail.position = position.short_name if position is not None else None

    season = db.get(Season, player.season_id)
    signal = classify_price_momentum(
        transfers_in_event=player.transfers_in_event,
        transfers_out_event=player.transfers_out_event,
        selected_by_percent=player.selected_by_percent,
        total_players=season.total_players if season is not None else None,
    )
    detail.price_momentum = PlayerPriceMomentumOut(
        momentum=signal.momentum,
        net_transfers_event=signal.net_transfers_event,
        estimated_owners=signal.estimated_owners,
        momentum_ratio=signal.momentum_ratio,
    )
    return detail


@router.get("/{player_id}/price-history", response_model=list[PlayerPriceChangeOut])
def get_player_price_history(
    player_id: uuid.UUID, db: Session = Depends(get_db)
) -> list[PlayerPriceChangeOut]:
    """Most-recent-first list of every OBSERVED price change for this player, this season.

    Empty (not a 404) for a player with no recorded changes yet -- a
    perfectly normal state for most of the season, not an error (see
    ``packages.database.models.player_price_history``'s own docstring: a
    row only exists when ``app.ingestion.normalization.upsert_players``
    actually observed ``now_cost`` differ between two ingestion runs).
    """
    if db.get(Player, player_id) is None:
        raise HTTPException(status_code=404, detail=f"Player {player_id} not found.")

    rows = (
        db.execute(
            select(PlayerPriceHistory)
            .where(PlayerPriceHistory.player_id == player_id)
            .order_by(PlayerPriceHistory.observed_at.desc())
            .limit(_MAX_PRICE_HISTORY_RESULTS)
        )
        .scalars()
        .all()
    )
    return [
        PlayerPriceChangeOut(old_cost=row.old_cost, new_cost=row.new_cost, observed_at=row.observed_at)
        for row in rows
    ]


def _explain_projection(
    projection: PlayerProjection,
    *,
    ep_next: float | None,
    career_avg_points_prior_seasons: float | None,
    next_fixture_difficulty: int | None,
    champion_model_family: str | None,
) -> str:
    """A one-line, plain-English account of which code path produced ``projection`` and why --
    the "why this projection" breakdown's human-readable summary. Branches on ``projection.source``
    (see ``app.optimization.projection``'s own source strings) rather than re-deriving state,
    since the source string already IS the authoritative record of which path ran."""
    if projection.source == "unavailable_status_excluded":
        return "This player is currently unavailable, so no points are projected."
    if projection.source == "cold_start_blend_ep_next_and_career_avg":
        difficulty_note = (
            f", adjusted for a next-fixture difficulty of {next_fixture_difficulty}/5"
            if next_fixture_difficulty is not None
            else ""
        )
        return (
            f"First gameweek of a new season -- no current-season form yet, so this blends "
            f"FPL's own estimate ({ep_next}) with this player's real prior-season average "
            f"({career_avg_points_prior_seasons}){difficulty_note}."
        )
    if champion_model_family is not None:
        return f"Prediction from the trained champion model ({champion_model_family})."
    scaled_note = " (scaled down for a reduced chance of playing)" if "scaled" in projection.source else ""
    if projection.source in ("ep_next", "ep_next_availability_scaled"):
        return f"FPL's own forward-looking estimate (ep_next){scaled_note}."
    if projection.source in ("points_per_game", "points_per_game_availability_scaled"):
        return (
            f"No ep_next estimate available; using this season's points-per-game average "
            f"instead{scaled_note}."
        )
    return "No projection data is available for this player yet."


@router.get("/{player_id}/projection", response_model=PlayerProjectionResponse)
def get_player_projection(player_id: uuid.UUID, db: Session = Depends(get_db)) -> PlayerProjectionResponse:
    """Champion-aware live projection — mirrors exactly what the optimizer would actually use.

    Resolves a target gameweek scoped to the player's own season (same
    ``resolve_target_gameweek`` helper the optimizer jobs use) and, if one
    resolves and a champion model exists, uses
    ``project_player_with_champion``; falls back to the plain
    ``project_player`` formula otherwise (no active gameweek, or no
    champion yet) — identical fallback behavior to the optimizer jobs.

    Also returns the "why this projection" breakdown (added post-review): the
    real inputs already computed behind ``projected_points`` -- for a
    cold-start season, the ``ep_next``/``career_avg_points_prior_seasons``/
    ``next_fixture_difficulty`` values ``_cold_start_projection`` actually
    blended; for a warm season with a champion-sourced prediction, which
    model family/version produced it. Deliberately NOT full feature-level
    attribution (no SHAP, no per-feature contribution breakdown) -- these are
    the same few numbers the projection formula itself already computes, just
    surfaced rather than recomputed or approximated.
    """
    player = db.get(Player, player_id)
    if player is None:
        raise HTTPException(status_code=404, detail=f"Player {player_id} not found.")

    target_gameweek_id, _ = resolve_target_gameweek(db, player.season_id)
    champion = None
    cold_start = False
    if target_gameweek_id is None:
        projection = project_player(player)
    else:
        champion = load_current_champion(db)
        cold_start = is_cold_start_season(db, player.season_id)
        projection = project_player_with_champion(db, player, target_gameweek_id, champion)

    # Best-effort display-only lookup of the two cold-start blend inputs (and the schedule-fact
    # next_fixture_difficulty, which is meaningful regardless of cold-start) -- never allowed to
    # fail this endpoint, mirroring _cold_start_projection's own graceful-degrade discipline.
    career_avg_points_prior_seasons = None
    next_fixture_difficulty = None
    if target_gameweek_id is not None:
        try:
            feature_row = compute_features_for_player_gameweek(db, player.id, target_gameweek_id)
            career_avg_points_prior_seasons = feature_row.career_avg_points_prior_seasons
            next_fixture_difficulty = feature_row.next_fixture_difficulty
        except Exception:  # noqa: BLE001 - this breakdown must never fail the endpoint
            logger.warning(
                "Could not compute feature row for projection breakdown "
                "player_id=%s target_gameweek_id=%s.",
                player.id,
                target_gameweek_id,
                exc_info=True,
            )

    champion_model_family = None
    champion_model_version_id = None
    if champion is not None and projection.source == f"ml_champion_{champion.model_family.value}":
        champion_model_family = champion.model_family.value
        champion_model_version_id = champion.model_version_id

    explanation = _explain_projection(
        projection,
        ep_next=float(player.ep_next) if player.ep_next is not None else None,
        career_avg_points_prior_seasons=(
            float(career_avg_points_prior_seasons) if career_avg_points_prior_seasons is not None else None
        ),
        next_fixture_difficulty=next_fixture_difficulty,
        champion_model_family=champion_model_family,
    )

    return PlayerProjectionResponse(
        player_id=player.id,
        projected_points=projection.projected_points,
        source=projection.source,
        is_eligible=projection.is_eligible,
        projection_formula_version=PROJECTION_FORMULA_VERSION,
        chance_of_playing_next_round=player.chance_of_playing_next_round,
        status=player.status,
        is_cold_start_season=cold_start,
        ep_next=player.ep_next,
        career_avg_points_prior_seasons=career_avg_points_prior_seasons,
        next_fixture_difficulty=next_fixture_difficulty,
        champion_model_family=champion_model_family,
        champion_model_version_id=champion_model_version_id,
        explanation=explanation,
    )
