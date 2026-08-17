"""TEAM_STRENGTH_SYNC job: pulls real, continuously-updated Elo ratings from ClubElo.com for the
current season's 20 Premier League teams.

Real gap investigated: FPL's own ``next_fixture_difficulty`` is a coarse, editorially-set 1-5
integer that doesn't move between fixtures the way real team form does. ClubElo publishes a real,
match-result-derived Elo rating per club, continuously updated. Real validation
(``scripts/validate_elo_fixture_adjustment.py``) found it does NOT improve
``app.optimization.projection._cold_start_projection``'s own fixture adjustment over the existing
FPL-difficulty one -- that formula still uses only ``next_fixture_difficulty``, unchanged. This
sync still has real value: ``teams.elo_rating``/``player_features.next_fixture_elo_differential``
reach the TRAINED champion model's own feature matrix automatically
(``app.modeling.candidates``), where it can learn its own weight for the signal.

No dedicated run-audit table -- this is a simple upsert-style sync (one HTTP call, one CSV parse,
up to 20 row updates), the same weight class as this codebase's other lightweight syncs; a
dedicated table would be overkill for something this small and side-effect-free to re-run. Never
fails the whole run over one team's unresolved name -- see ``_upsert_team_elo_ratings``, mirrors
this codebase's established "graceful, per-item degradation over an external, best-effort signal"
precedent (e.g. ``LeagueSyncJobRunner``'s per-rival failure isolation).
"""

import logging
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ingestion.team_strength import (
    parse_ratings_csv,
    premier_league_ratings_by_club,
    resolve_clubelo_name,
)
from app.jobs.registry import register_handler
from app.jobs.runner import JobContext, JobResult, JobRunner
from packages.clubelo_client.client import ClubEloClient, today_iso_date
from packages.database.models.season import Season
from packages.database.models.team import Team
from packages.database.session import SessionLocal
from packages.domain.jobs import JobStatus, JobType

logger = logging.getLogger(__name__)


def _resolve_current_season(session: Session) -> Season | None:
    return session.execute(select(Season).where(Season.is_current.is_(True))).scalar_one_or_none()


def _upsert_team_elo_ratings(
    session: Session, teams: list[Team], ratings_by_club: dict[str, Decimal]
) -> tuple[int, list[str]]:
    """Updates ``elo_rating``/``elo_last_synced_at`` in place for every team resolvable against
    ``ratings_by_club`` (ClubElo club name -> Elo). Returns ``(matched_count,
    unmatched_team_names)``; never raises for an individual unmatched team -- that team's
    existing ``elo_rating`` is simply left untouched (stale-but-present is safer than clobbering
    a real prior value with nothing)."""
    now = datetime.now(UTC)
    matched = 0
    unmatched: list[str] = []
    for team in teams:
        clubelo_name = resolve_clubelo_name(team.name)
        rating = ratings_by_club.get(clubelo_name)
        if rating is None:
            unmatched.append(team.name)
            continue
        team.elo_rating = rating
        team.elo_last_synced_at = now
        matched += 1
    return matched, unmatched


@register_handler(JobType.TEAM_STRENGTH_SYNC)
class TeamStrengthSyncJobRunner(JobRunner):
    """Syncs real ClubElo.com Elo ratings onto the current season's ``teams`` rows."""

    def run(self, ctx: JobContext) -> JobResult:
        session = SessionLocal()
        try:
            season = _resolve_current_season(session)
            if season is None:
                return JobResult(
                    status=JobStatus.FAILED,
                    output=None,
                    error_code="NO_CURRENT_SEASON",
                    error_message="No current season found.",
                )

            as_of_date = (ctx.parameters or {}).get("as_of_date") or today_iso_date()

            with ClubEloClient() as client:
                fetch_result = client.fetch_ratings_snapshot(as_of_date)

            if fetch_result.raw_csv is None:
                logger.warning(
                    "team_strength_sync.fetch_failed",
                    extra={
                        "as_of_date": as_of_date,
                        "http_status": fetch_result.http_status,
                        "error": fetch_result.error_message,
                    },
                )
                return JobResult(
                    status=JobStatus.FAILED,
                    output=None,
                    error_code="CLUBELO_FETCH_FAILED",
                    error_message=(
                        f"Could not fetch ClubElo ratings for {as_of_date}: "
                        f"{fetch_result.error_message}"
                    ),
                )

            ratings_by_club = premier_league_ratings_by_club(parse_ratings_csv(fetch_result.raw_csv))

            teams = (
                session.execute(select(Team).where(Team.season_id == season.id)).scalars().all()
            )
            matched, unmatched = _upsert_team_elo_ratings(session, list(teams), ratings_by_club)
            session.commit()

            return JobResult(
                status=JobStatus.SUCCEEDED,
                output={
                    "as_of_date": as_of_date,
                    "season_label": season.label,
                    "teams_matched": matched,
                    "teams_unmatched": unmatched,
                    "clubelo_clubs_seen": len(ratings_by_club),
                },
                error_code=None,
                error_message=None,
            )
        finally:
            session.close()
