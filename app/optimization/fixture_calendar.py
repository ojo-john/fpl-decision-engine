"""Blank/Double Gameweek detection: per-team fixture counts for a gameweek (or several).

Generic fixture-calendar arithmetic with no chip-specific concept -- lives
alongside ``gameweek_resolution.py`` (not under ``app.chips``) for the same
reason that module's docstring gives for its own home: this is "resolve X
fact about a gameweek from the DB" plumbing that any caller (chip planning,
transfer planning, lineup planning) can reuse, not something intrinsically
about chips. Matches ``gameweek_resolution.py``'s conventions: small
``Session``-taking helper functions, one clearly-documented query shape,
graceful handling of the realistic gaps in FPL's own fixture data rather
than silently working around them.

Known, accepted freshness limitation: ``Fixture.gameweek_id`` is nullable --
per that model's own docstring, a postponed/rescheduled/TBD fixture can have
``event=null`` in the raw FPL data until it is reassigned to a real
gameweek. Such a fixture correctly (and silently) drops out of BOTH teams'
fixture counts here until it is re-ingested with a resolved
``gameweek_id`` -- there is no reliable way to guess which gameweek an
unresolved fixture belongs to, so this module does not attempt to work
around it. A caller relying on this for Blank Gameweek detection should be
aware the count for an affected team could rise once the fixture is
rescheduled and re-ingested.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.database.models.fixture import Fixture
from packages.database.models.team import Team


@dataclass(frozen=True)
class TeamFixtureCount:
    """One team's fixture count for a single gameweek.

    ``fixture_count``: 0 = blank, 1 = normal, 2+ = double. Callers should
    write the trivial `.fixture_count == 0` / `.fixture_count >= 2`
    predicates themselves rather than this module adding one-line
    `is_blank`/`is_double` wrapper functions for it.
    """

    team_id: uuid.UUID
    fixture_count: int
    fixture_ids: tuple[uuid.UUID, ...]


def _team_ids_for_season(session: Session, season_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        session.execute(select(Team.id).where(Team.season_id == season_id, Team.unavailable.is_(False)))
        .scalars()
        .all()
    )


def _non_quarantined_fixtures_for_gameweeks(
    session: Session, season_id: uuid.UUID, gameweek_ids: list[uuid.UUID]
) -> list[Fixture]:
    # Ordered by fpl_fixture_id (a stable, meaningful business key) so a
    # team's TeamFixtureCount.fixture_ids has a deterministic order -- an
    # unordered query has no obligation to return rows in insertion order,
    # and drifts as the underlying table grows (same bug class as the
    # player_features cross-test pollution fixed for MODEL_TRAINING/BACKTEST).
    return list(
        session.execute(
            select(Fixture)
            .where(
                Fixture.season_id == season_id,
                Fixture.gameweek_id.in_(gameweek_ids),
                Fixture.is_quarantined.is_(False),
            )
            .order_by(Fixture.fpl_fixture_id)
        )
        .scalars()
        .all()
    )


def _counts_from_fixtures(
    team_ids: list[uuid.UUID], fixtures: list[Fixture]
) -> dict[uuid.UUID, TeamFixtureCount]:
    fixture_ids_by_team: dict[uuid.UUID, list[uuid.UUID]] = {team_id: [] for team_id in team_ids}
    for fixture in fixtures:
        for team_id in (fixture.home_team_id, fixture.away_team_id):
            if team_id in fixture_ids_by_team:
                fixture_ids_by_team[team_id].append(fixture.id)
    return {
        team_id: TeamFixtureCount(team_id, len(fixture_ids), tuple(fixture_ids))
        for team_id, fixture_ids in fixture_ids_by_team.items()
    }


def resolve_team_fixture_counts_for_gameweek(
    session: Session, season_id: uuid.UUID, gameweek_id: uuid.UUID
) -> dict[uuid.UUID, TeamFixtureCount]:
    """Every non-quarantined Team in the season mapped to its ``TeamFixtureCount`` for one gameweek.

    A team with zero matching fixtures is STILL present in the returned dict
    with ``fixture_count=0`` -- never silently absent (an absent key would be
    ambiguous with "this team isn't in the season at all").
    """
    return resolve_team_fixture_counts_for_gameweeks(session, season_id, [gameweek_id])[gameweek_id]


def resolve_team_fixture_counts_for_gameweeks(
    session: Session, season_id: uuid.UUID, gameweek_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[uuid.UUID, TeamFixtureCount]]:
    """Batched version of ``resolve_team_fixture_counts_for_gameweek`` across several gameweeks.

    One query for all teams, one query for all matching fixtures across
    every requested gameweek -- not N queries.
    """
    team_ids = _team_ids_for_season(session, season_id)
    if not gameweek_ids:
        return {}

    fixtures = _non_quarantined_fixtures_for_gameweeks(session, season_id, gameweek_ids)
    fixtures_by_gameweek: dict[uuid.UUID, list[Fixture]] = {gameweek_id: [] for gameweek_id in gameweek_ids}
    for fixture in fixtures:
        if fixture.gameweek_id in fixtures_by_gameweek:
            fixtures_by_gameweek[fixture.gameweek_id].append(fixture)

    return {
        gameweek_id: _counts_from_fixtures(team_ids, gameweek_fixtures)
        for gameweek_id, gameweek_fixtures in fixtures_by_gameweek.items()
    }
