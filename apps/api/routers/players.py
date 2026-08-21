"""GET /players endpoints, scoped to the current season only."""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.features.service import compute_features_for_player_gameweek
from app.modeling.cold_start_validation import defensive_contribution_hit_rate_prior_seasons
from app.optimization.champion_loader import load_current_champion
from app.optimization.gameweek_resolution import resolve_target_gameweek
from app.optimization.projection import (
    AVAILABILITY_RISK_NOTED_SUFFIX,
    PROJECTION_FORMULA_VERSION,
    ROLE_BOOSTED_SUFFIX,
    PlayerProjection,
    get_active_availability_risk_note,
    get_active_role_boost_note,
    is_cold_start_player,
    is_cold_start_season,
    project_player,
    project_player_with_champion,
)
from app.players.price_signal import classify_price_momentum
from apps.api.dependencies import get_db
from apps.api.schemas.players import (
    ActiveNoteOut,
    AvailabilityRiskNoteCreate,
    AvailabilityRiskNoteOut,
    PlayerDetail,
    PlayerListResponse,
    PlayerPriceChangeOut,
    PlayerPriceMomentumOut,
    PlayerProjectionResponse,
    PlayerSummary,
    RoleNoteCreate,
    RoleNoteOut,
)
from packages.database.models.gameweek import Gameweek
from packages.database.models.player import Player
from packages.database.models.player_availability_risk_note import PlayerAvailabilityRiskNote
from packages.database.models.player_price_history import PlayerPriceHistory
from packages.database.models.player_role_note import PlayerRoleNote
from packages.database.models.position import Position
from packages.database.models.price_signal_calibration import PriceSignalCalibration
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

    # Loaded once per request, not once per row (up to MAX_PAGE_SIZE=200 players) -- same
    # active-calibration lookup get_player's own price_momentum enrichment does.
    total_players = db.execute(select(Season.total_players).where(Season.id == season_id)).scalar_one()
    active_calibration = db.execute(
        select(PriceSignalCalibration).where(PriceSignalCalibration.is_active.is_(True))
    ).scalar_one_or_none()

    items = []
    for p in rows:
        signal = classify_price_momentum(
            transfers_in_event=p.transfers_in_event,
            transfers_out_event=p.transfers_out_event,
            selected_by_percent=p.selected_by_percent,
            total_players=total_players,
            ratio_threshold=active_calibration.ratio_threshold if active_calibration is not None else None,
            absolute_floor=active_calibration.absolute_floor if active_calibration is not None else None,
        )
        items.append(
            PlayerSummary(
                id=p.id,
                fpl_element_id=p.fpl_element_id,
                web_name=p.web_name,
                team_id=p.team_id,
                team_short_name=teams_by_id[p.team_id].short_name if p.team_id in teams_by_id else None,
                position=(
                    positions_by_id[p.position_id].short_name if p.position_id in positions_by_id else None
                ),
                now_cost=p.now_cost,
                status=p.status,
                total_points=p.total_points,
                form=p.form,
                points_per_game=p.points_per_game,
                ep_next=p.ep_next,
                is_quarantined=p.is_quarantined,
                price_momentum=PlayerPriceMomentumOut(
                    momentum=signal.momentum,
                    net_transfers_event=signal.net_transfers_event,
                    estimated_owners=signal.estimated_owners,
                    momentum_ratio=signal.momentum_ratio,
                ),
            )
        )
    return PlayerListResponse(items=items, page=page, page_size=page_size, total=total)


def _resolve_relevant_fpl_event_id(db: Session, season_id: uuid.UUID) -> int | None:
    """The gameweek number "active" note-window checks are evaluated against -- the next
    unfinished gameweek, mirroring ``apps.api.routers.gameweeks.get_next_deadline``'s own
    resolution exactly (the real squad-selection deadline is what matters, independent of the
    optimization gate). ``None`` off-season (every gameweek already finished)."""
    gameweek = db.execute(
        select(Gameweek)
        .where(Gameweek.season_id == season_id, Gameweek.finished.is_(False))
        .order_by(Gameweek.fpl_event_id)
        .limit(1)
    ).scalar_one_or_none()
    return gameweek.fpl_event_id if gameweek is not None else None


def _is_note_active(fpl_event_id: int, valid_from: int | None, valid_until: int | None) -> bool:
    """Same inclusive-window check as ``app.optimization.projection``'s own
    ``get_active_availability_risk_note``/``get_active_role_boost_note`` -- kept as a tiny,
    duplicated pure function here rather than imported, since those two are scoped to a single
    player/gameweek pair and already do their own DB query; this endpoint evaluates the window
    against every note across every player for one already-resolved gameweek number instead."""
    return (valid_from is None or fpl_event_id >= valid_from) and (
        valid_until is None or fpl_event_id <= valid_until
    )


@router.get("/notes/active", response_model=list[ActiveNoteOut])
def list_active_notes(db: Session = Depends(get_db)) -> list[ActiveNoteOut]:
    """Every currently-active availability-risk and role-boost note across every player, for the
    current/next unfinished gameweek -- NOT player-scoped, unlike every other note endpoint on
    this router. Built for external, read-only consumers that can't reach this app's database
    directly (see ``.github/workflows/scout-watch.yml``'s dedup step) to know what's already
    known before proposing something as new. Registered before ``/{player_id}`` so ``notes`` can
    never be misread as a player id (same defensive route-ordering discipline as
    ``apps.api.routers.optimization``'s own chips-vs-catch-all precedent -- see that router's own
    comment -- even though these two paths don't actually collide in segment count).

    Returns an empty list (not 404) when there's no current season or no unfinished gameweek to
    evaluate against (off-season) -- there's nothing "active" to report in either case, and a
    consuming script shouldn't need special-case 404 handling for what is, for its purposes, the
    same as "nothing found".
    """
    season_id = db.execute(select(Season.id).where(Season.is_current.is_(True))).scalar_one_or_none()
    if season_id is None:
        return []

    fpl_event_id = _resolve_relevant_fpl_event_id(db, season_id)
    if fpl_event_id is None:
        return []

    players_by_id = {
        p.id: p for p in db.execute(select(Player).where(Player.season_id == season_id)).scalars()
    }

    results: list[ActiveNoteOut] = []

    risk_notes = (
        db.execute(
            select(PlayerAvailabilityRiskNote).where(PlayerAvailabilityRiskNote.player_id.in_(players_by_id))
        )
        .scalars()
        .all()
    )
    for risk_note in risk_notes:
        if not _is_note_active(
            fpl_event_id, risk_note.valid_from_fpl_event_id, risk_note.valid_until_fpl_event_id
        ):
            continue
        player = players_by_id.get(risk_note.player_id)
        results.append(
            ActiveNoteOut(
                player_id=risk_note.player_id,
                web_name=player.web_name if player is not None else None,
                category="availability_risk",
                role=None,
                reason=risk_note.reason,
                severity=risk_note.severity,
                valid_from_fpl_event_id=risk_note.valid_from_fpl_event_id,
                valid_until_fpl_event_id=risk_note.valid_until_fpl_event_id,
                created_at=risk_note.created_at,
                created_by=risk_note.created_by,
            )
        )

    role_notes = (
        db.execute(select(PlayerRoleNote).where(PlayerRoleNote.player_id.in_(players_by_id))).scalars().all()
    )
    for role_note in role_notes:
        if not _is_note_active(
            fpl_event_id, role_note.valid_from_fpl_event_id, role_note.valid_until_fpl_event_id
        ):
            continue
        player = players_by_id.get(role_note.player_id)
        results.append(
            ActiveNoteOut(
                player_id=role_note.player_id,
                web_name=player.web_name if player is not None else None,
                category="role_boost",
                role=role_note.role,
                reason=role_note.reason,
                severity=role_note.severity,
                valid_from_fpl_event_id=role_note.valid_from_fpl_event_id,
                valid_until_fpl_event_id=role_note.valid_until_fpl_event_id,
                created_at=role_note.created_at,
                created_by=role_note.created_by,
            )
        )

    return results


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
    active_calibration = db.execute(
        select(PriceSignalCalibration).where(PriceSignalCalibration.is_active.is_(True))
    ).scalar_one_or_none()
    signal = classify_price_momentum(
        transfers_in_event=player.transfers_in_event,
        transfers_out_event=player.transfers_out_event,
        selected_by_percent=player.selected_by_percent,
        total_players=season.total_players if season is not None else None,
        ratio_threshold=active_calibration.ratio_threshold if active_calibration is not None else None,
        absolute_floor=active_calibration.absolute_floor if active_calibration is not None else None,
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


def _explain_base_projection(
    projection: PlayerProjection,
    *,
    ep_next: float | None,
    career_avg_points_prior_seasons: float | None,
    career_avg_early_season_points_prior_seasons: float | None,
    next_fixture_difficulty: int | None,
    champion_model_family: str | None,
    is_cold_start_season: bool,
) -> str:
    """Same as ``_explain_projection`` but assumes ``projection.source`` has already had any
    ``AVAILABILITY_RISK_NOTED_SUFFIX`` stripped -- see that function for why."""
    if projection.source == "unavailable_status_excluded":
        return "This player is currently unavailable, so no points are projected."
    if projection.source == "cold_start_blend_ep_next_and_career_avg":
        difficulty_note = (
            f", adjusted for a next-fixture difficulty of {next_fixture_difficulty}/5"
            if next_fixture_difficulty is not None
            else ""
        )
        # _cold_start_projection prefers the early-season-specific average over the plain
        # full-season one whenever it exists -- mirror that same preference here so this text
        # always describes the number that was actually blended, not a stale/wrong one.
        if career_avg_early_season_points_prior_seasons is not None:
            career_avg_note = (
                f"this player's own early-season average from prior seasons "
                f"({career_avg_early_season_points_prior_seasons})"
            )
        else:
            career_avg_note = f"this player's real prior-season average ({career_avg_points_prior_seasons})"
        # is_cold_start_season distinguishes WHY this player is on the cold-start path: a genuine
        # season-wide first gameweek (nobody has data yet) versus this specific player having no
        # current-season data of their own mid-season (a late debut, a return from a long
        # absence) -- see app.optimization.projection._is_cold_start_player. Both take the exact
        # same blend, but "first gameweek of a new season" would be a misleading thing to tell a
        # manager about a player in gameweek 15.
        lead_in = (
            "First gameweek of a new season -- no current-season form yet"
            if is_cold_start_season
            else "No current-season data yet for this player (e.g. a new signing or a long absence)"
        )
        return (
            f"{lead_in}, so this blends FPL's own estimate ({ep_next}) with "
            f"{career_avg_note}{difficulty_note}."
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


def _explain_projection(
    projection: PlayerProjection,
    *,
    ep_next: float | None,
    career_avg_points_prior_seasons: float | None,
    career_avg_early_season_points_prior_seasons: float | None,
    next_fixture_difficulty: int | None,
    champion_model_family: str | None,
    is_cold_start_season: bool,
    availability_risk_reason: str | None,
    role_boost_reason: str | None,
) -> str:
    """A one-line, plain-English account of which code path produced ``projection`` and why --
    the "why this projection" breakdown's human-readable summary. Branches on ``projection.source``
    (see ``app.optimization.projection``'s own source strings) rather than re-deriving state,
    since the source string already IS the authoritative record of which path ran.

    ``app.optimization.projection._apply_availability_risk_notes``/``_apply_role_boost_notes`` can
    each append their own suffix to whatever source the underlying formula produced (in that
    order -- risk suffix first, then role suffix, matching ``project_player_with_champion``'s own
    call order) -- both stripped here before delegating to ``_explain_base_projection`` (so every
    existing branch keeps matching exactly), then a note-specific sentence is appended per suffix
    actually present, since a discounted/boosted number with no explanation of WHY would be more
    confusing than the un-noted case."""
    has_role_boost = projection.source.endswith(ROLE_BOOSTED_SUFFIX)
    source_without_role = (
        projection.source[: -len(ROLE_BOOSTED_SUFFIX)] if has_role_boost else projection.source
    )
    has_risk_note = source_without_role.endswith(AVAILABILITY_RISK_NOTED_SUFFIX)
    base_source = (
        source_without_role[: -len(AVAILABILITY_RISK_NOTED_SUFFIX)] if has_risk_note else source_without_role
    )
    base_projection = PlayerProjection(
        projection.player_id, projection.projected_points, base_source, projection.is_eligible
    )
    explanation = _explain_base_projection(
        base_projection,
        ep_next=ep_next,
        career_avg_points_prior_seasons=career_avg_points_prior_seasons,
        career_avg_early_season_points_prior_seasons=career_avg_early_season_points_prior_seasons,
        next_fixture_difficulty=next_fixture_difficulty,
        champion_model_family=champion_model_family,
        is_cold_start_season=is_cold_start_season,
    )
    if has_risk_note and availability_risk_reason is not None:
        explanation = f"{explanation} Flagged availability risk: {availability_risk_reason}"
    if has_role_boost and role_boost_reason is not None:
        explanation = f"{explanation} Boosted for a confirmed role: {role_boost_reason}"
    return explanation


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

    Also surfaces ``defensive_contribution_hit_rate_prior_seasons``/``..._games`` (DEF/MID only):
    this player's real historical rate of hitting FPL's 2025-26+ defensive-contribution bonus
    threshold, via ``app.modeling.cold_start_validation
    .defensive_contribution_hit_rate_prior_seasons``. PURELY INFORMATIONAL -- unlike every other
    field here, this one is NOT an input ``_cold_start_projection`` actually used; there is
    currently no real, leakage-safe evidence to justify wiring it into the formula (see that
    function's own docstring). Surfaced anyway so a reader can see the same real signal directly.
    """
    player = db.get(Player, player_id)
    if player is None:
        raise HTTPException(status_code=404, detail=f"Player {player_id} not found.")

    target_gameweek_id, _ = resolve_target_gameweek(db, player.season_id)
    champion = None
    cold_start_season = False
    cold_start_player = False
    if target_gameweek_id is None:
        projection = project_player(player)
    else:
        champion = load_current_champion(db)
        cold_start_season = is_cold_start_season(db, player.season_id)
        cold_start_player = is_cold_start_player(db, player.id)
        projection = project_player_with_champion(db, player, target_gameweek_id, champion)

    # Best-effort display-only lookup of the two cold-start blend inputs (and the schedule-fact
    # next_fixture_difficulty, which is meaningful regardless of cold-start) -- never allowed to
    # fail this endpoint, mirroring _cold_start_projection's own graceful-degrade discipline.
    career_avg_points_prior_seasons = None
    career_avg_early_season_points_prior_seasons = None
    next_fixture_difficulty = None
    dc_hit_rate = None
    dc_hit_rate_games = None
    if target_gameweek_id is not None:
        try:
            feature_row = compute_features_for_player_gameweek(db, player.id, target_gameweek_id)
            career_avg_points_prior_seasons = feature_row.career_avg_points_prior_seasons
            career_avg_early_season_points_prior_seasons = (
                feature_row.career_avg_early_season_points_prior_seasons
            )
            next_fixture_difficulty = feature_row.next_fixture_difficulty
            if player.fpl_code is not None and feature_row.position_short_name is not None:
                dc_result = defensive_contribution_hit_rate_prior_seasons(
                    db, player.fpl_code, feature_row.position_short_name, feature_row.cutoff_timestamp
                )
                if dc_result is not None:
                    dc_hit_rate, dc_hit_rate_games = dc_result
        except Exception:  # noqa: BLE001 - this breakdown must never fail the endpoint
            logger.warning(
                "Could not compute feature row for projection breakdown player_id=%s target_gameweek_id=%s.",
                player.id,
                target_gameweek_id,
                exc_info=True,
            )

    champion_model_family = None
    champion_model_version_id = None
    # .startswith(), not == -- AVAILABILITY_RISK_NOTED_SUFFIX may be appended after the
    # "ml_champion_{family}" source string when a risk note is also active for this gameweek.
    if champion is not None and projection.source.startswith(f"ml_champion_{champion.model_family.value}"):
        champion_model_family = champion.model_family.value
        champion_model_version_id = champion.model_version_id

    active_risk_note = (
        get_active_availability_risk_note(db, player.id, target_gameweek_id)
        if target_gameweek_id is not None
        else None
    )
    active_role_note = (
        get_active_role_boost_note(db, player.id, target_gameweek_id)
        if target_gameweek_id is not None
        else None
    )

    explanation = _explain_projection(
        projection,
        ep_next=float(player.ep_next) if player.ep_next is not None else None,
        career_avg_points_prior_seasons=(
            float(career_avg_points_prior_seasons) if career_avg_points_prior_seasons is not None else None
        ),
        career_avg_early_season_points_prior_seasons=(
            float(career_avg_early_season_points_prior_seasons)
            if career_avg_early_season_points_prior_seasons is not None
            else None
        ),
        next_fixture_difficulty=next_fixture_difficulty,
        champion_model_family=champion_model_family,
        is_cold_start_season=cold_start_season,
        availability_risk_reason=active_risk_note.reason if active_risk_note is not None else None,
        role_boost_reason=active_role_note.reason if active_role_note is not None else None,
    )

    return PlayerProjectionResponse(
        player_id=player.id,
        projected_points=projection.projected_points,
        source=projection.source,
        is_eligible=projection.is_eligible,
        projection_formula_version=PROJECTION_FORMULA_VERSION,
        chance_of_playing_next_round=player.chance_of_playing_next_round,
        status=player.status,
        is_cold_start_season=cold_start_season,
        is_cold_start_player=cold_start_player,
        ep_next=player.ep_next,
        career_avg_points_prior_seasons=career_avg_points_prior_seasons,
        career_avg_early_season_points_prior_seasons=career_avg_early_season_points_prior_seasons,
        next_fixture_difficulty=next_fixture_difficulty,
        champion_model_family=champion_model_family,
        champion_model_version_id=champion_model_version_id,
        availability_risk_reason=active_risk_note.reason if active_risk_note is not None else None,
        availability_risk_severity=active_risk_note.severity if active_risk_note is not None else None,
        role_boost_reason=active_role_note.reason if active_role_note is not None else None,
        role_boost_severity=active_role_note.severity if active_role_note is not None else None,
        defensive_contribution_hit_rate_prior_seasons=dc_hit_rate,
        defensive_contribution_hit_rate_prior_games=dc_hit_rate_games,
        explanation=explanation,
    )


@router.get("/{player_id}/availability-risk-notes", response_model=list[AvailabilityRiskNoteOut])
def list_availability_risk_notes(
    player_id: uuid.UUID, db: Session = Depends(get_db)
) -> list[AvailabilityRiskNoteOut]:
    """Every note ever recorded for this player, past and present, most-recent-first --
    transparency/audit, not just the currently-active one (see ``GET /{player_id}/projection``
    for what's actually applied to THIS gameweek's projection)."""
    if db.get(Player, player_id) is None:
        raise HTTPException(status_code=404, detail=f"Player {player_id} not found.")

    rows = (
        db.execute(
            select(PlayerAvailabilityRiskNote)
            .where(PlayerAvailabilityRiskNote.player_id == player_id)
            .order_by(PlayerAvailabilityRiskNote.created_at.desc())
        )
        .scalars()
        .all()
    )
    return [AvailabilityRiskNoteOut.model_validate(row) for row in rows]


@router.post(
    "/{player_id}/availability-risk-notes",
    response_model=AvailabilityRiskNoteOut,
    status_code=201,
)
def create_availability_risk_note(
    player_id: uuid.UUID, body: AvailabilityRiskNoteCreate, db: Session = Depends(get_db)
) -> AvailabilityRiskNoteOut:
    """Create a judgment-based availability-risk note for this player -- see
    ``packages.database.models.player_availability_risk_note``'s own docstring for why this is
    NOT the same thing as FPL's own official injury/doubt data (that already flows in
    automatically; this is for a real, sourced risk FPL hasn't flagged yet)."""
    player = db.get(Player, player_id)
    if player is None:
        raise HTTPException(status_code=404, detail=f"Player {player_id} not found.")

    if (
        body.valid_from_fpl_event_id is not None
        and body.valid_until_fpl_event_id is not None
        and body.valid_from_fpl_event_id > body.valid_until_fpl_event_id
    ):
        raise HTTPException(
            status_code=422, detail="valid_from_fpl_event_id must not be after valid_until_fpl_event_id."
        )

    note = PlayerAvailabilityRiskNote(
        player_id=player_id,
        season_id=player.season_id,
        reason=body.reason,
        severity=body.severity,
        valid_from_fpl_event_id=body.valid_from_fpl_event_id,
        valid_until_fpl_event_id=body.valid_until_fpl_event_id,
        created_by=body.created_by,
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    return AvailabilityRiskNoteOut.model_validate(note)


@router.delete("/{player_id}/availability-risk-notes/{note_id}", status_code=204)
def delete_availability_risk_note(
    player_id: uuid.UUID, note_id: uuid.UUID, db: Session = Depends(get_db)
) -> None:
    """Remove a note -- e.g. it turns out to be wrong, or real news supersedes it."""
    note = db.get(PlayerAvailabilityRiskNote, note_id)
    if note is None or note.player_id != player_id:
        raise HTTPException(status_code=404, detail=f"Availability risk note {note_id} not found.")
    db.delete(note)
    db.commit()


@router.get("/{player_id}/role-notes", response_model=list[RoleNoteOut])
def list_role_notes(player_id: uuid.UUID, db: Session = Depends(get_db)) -> list[RoleNoteOut]:
    """Every role note ever recorded for this player, past and present, most-recent-first --
    transparency/audit, not just the currently-active one (see ``GET /{player_id}/projection``
    for what's actually applied to THIS gameweek's projection). Mirrors
    ``list_availability_risk_notes`` exactly -- see ``packages.database.models.player_role_note``
    for why this is a separate table/endpoint rather than the same one."""
    if db.get(Player, player_id) is None:
        raise HTTPException(status_code=404, detail=f"Player {player_id} not found.")

    rows = (
        db.execute(
            select(PlayerRoleNote)
            .where(PlayerRoleNote.player_id == player_id)
            .order_by(PlayerRoleNote.created_at.desc())
        )
        .scalars()
        .all()
    )
    return [RoleNoteOut.model_validate(row) for row in rows]


@router.post("/{player_id}/role-notes", response_model=RoleNoteOut, status_code=201)
def create_role_note(
    player_id: uuid.UUID, body: RoleNoteCreate, db: Session = Depends(get_db)
) -> RoleNoteOut:
    """Create a curated, sourced set-piece/role-duty note for this player -- see
    ``packages.database.models.player_role_note``'s own docstring for why this is a separate
    mechanism from availability-risk notes, not a reuse of that table."""
    player = db.get(Player, player_id)
    if player is None:
        raise HTTPException(status_code=404, detail=f"Player {player_id} not found.")

    if (
        body.valid_from_fpl_event_id is not None
        and body.valid_until_fpl_event_id is not None
        and body.valid_from_fpl_event_id > body.valid_until_fpl_event_id
    ):
        raise HTTPException(
            status_code=422, detail="valid_from_fpl_event_id must not be after valid_until_fpl_event_id."
        )

    note = PlayerRoleNote(
        player_id=player_id,
        season_id=player.season_id,
        role=body.role,
        reason=body.reason,
        severity=body.severity,
        valid_from_fpl_event_id=body.valid_from_fpl_event_id,
        valid_until_fpl_event_id=body.valid_until_fpl_event_id,
        created_by=body.created_by,
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    return RoleNoteOut.model_validate(note)


@router.delete("/{player_id}/role-notes/{note_id}", status_code=204)
def delete_role_note(player_id: uuid.UUID, note_id: uuid.UUID, db: Session = Depends(get_db)) -> None:
    """Remove a role note -- e.g. the role changed hands again, or the original source was wrong."""
    note = db.get(PlayerRoleNote, note_id)
    if note is None or note.player_id != player_id:
        raise HTTPException(status_code=404, detail=f"Role note {note_id} not found.")
    db.delete(note)
    db.commit()
