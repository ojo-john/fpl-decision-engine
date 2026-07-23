"""``/leagues`` endpoints: read already-persisted LEAGUE_SYNC output (Milestone 8 Layer 1).

LEAGUE_SYNC (Milestone 5 Layer 1/2) previously had zero REST exposure --
reachable only via ``scripts/run_job.py`` or the scheduled dispatcher. These
are plain, capped-result reads over already-persisted rows, mirroring
``apps.api.routers.learning``'s GET endpoints exactly -- no job execution
here, and no ``POST /leagues/sync`` trigger endpoint either (that's a
natural Layer 3 "interactive actions" follow-up, not this layer's concern).
"""

import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.dependencies import get_db
from apps.api.schemas.leagues import LeagueEntryOut, LeagueOut, RivalProfileOut
from packages.database.models.league import League
from packages.database.models.league_entry import LeagueEntry
from packages.database.models.rival_profile import RivalProfile
from packages.database.models.season import Season

router = APIRouter(prefix="/leagues", tags=["leagues"])

# Same "capped read, not full pagination" convention as apps.api.routers.learning --
# a real H2H league's rival/standings row count never approaches a size where
# offset/limit paging is actually needed.
_MAX_RESULTS = 200


def _league_not_found_response(league_id: uuid.UUID) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={"error_code": "LEAGUE_NOT_FOUND", "detail": f"leagues row {league_id} not found."},
    )


def _rival_profile_out(row: RivalProfile) -> RivalProfileOut:
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
    return [LeagueOut(league_id=row.id, fpl_league_id=row.fpl_league_id, name=row.name) for row in rows]


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
    return [_rival_profile_out(row) for row in rows]


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
