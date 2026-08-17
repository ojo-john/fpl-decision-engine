"""Pure parsing/mapping helpers for ClubElo.com ratings, kept separate from the job orchestration
in ``app.jobs.handlers.team_strength_sync`` so both are independently unit-testable (mirrors this
codebase's established "DB in, pure functions in a dedicated module, DB out" split -- see
``app.features.engineering``/``app.features.service`` for the precedent).

Name mapping is the real integration risk here, not the HTTP call: FPL's own team names
(``teams.name``, sourced from bootstrap-static) and ClubElo's club names diverge for several real
Premier League clubs (confirmed live against both APIs on 2026-08-17 -- see
``_FPL_NAME_TO_CLUBELO_NAME`` below for the exact, hand-verified differences). An unmapped team is
left with ``elo_rating=None`` rather than guessed at -- see this table's own "unknown, not zero"
convention, documented on ``packages.database.models.team.Team.elo_rating``.
"""

import csv
import io
from dataclasses import dataclass
from decimal import Decimal

# Hand-verified 2026-08-17 against real responses from both https://fantasy.premierleague.com
# /api/bootstrap-static/ (the `name` field) and http://api.clubelo.com/<date> (the `Club` column,
# filtered to Country=ENG, Level=1). Only entries where the two sources' names actually differ are
# listed -- an FPL name not present here is assumed to match ClubElo's name verbatim (true for the
# majority of clubs, e.g. "Arsenal", "Liverpool", "Chelsea", "Brighton", "Brentford", "Newcastle",
# "Aston Villa", "Bournemouth", "Everton", "Fulham", "Crystal Palace", "Leeds", "Sunderland").
_FPL_NAME_TO_CLUBELO_NAME: dict[str, str] = {
    "Man City": "Man City",  # identical -- listed for clarity, FPL already uses ClubElo's form
    "Man Utd": "Man United",
    "Spurs": "Tottenham",
    "Nott'm Forest": "Forest",
    "Coventry City": "Coventry",
    "Hull City": "Hull",
    "Ipswich Town": "Ipswich",
}


@dataclass(frozen=True)
class ClubEloRating:
    club: str
    country: str
    level: int
    elo: Decimal


def parse_ratings_csv(raw_csv: str) -> list[ClubEloRating]:
    """Parse a raw ClubElo ``/<date>`` CSV response into typed rows.

    Skips rows with a blank/non-numeric ``Elo`` (ClubElo returns some placeholder rows for
    clubs without a rating yet) rather than raising -- a malformed or partially-empty row must
    never abort parsing every other real row in the same response.
    """
    reader = csv.DictReader(io.StringIO(raw_csv))
    ratings: list[ClubEloRating] = []
    for row in reader:
        try:
            elo = Decimal(row["Elo"])
            level = int(row["Level"])
        except (KeyError, TypeError, ValueError, ArithmeticError):
            continue
        ratings.append(
            ClubEloRating(club=row["Club"], country=row["Country"], level=level, elo=elo)
        )
    return ratings


def premier_league_ratings_by_club(ratings: list[ClubEloRating]) -> dict[str, Decimal]:
    """Filter to England, top flight only (``Country == "ENG"``, ``Level == 1``) -- ClubElo's
    snapshot includes every club in every country's league pyramid, most of them irrelevant here."""
    return {r.club: r.elo for r in ratings if r.country == "ENG" and r.level == 1}


def resolve_clubelo_name(fpl_team_name: str) -> str:
    """FPL's ``teams.name`` -> the name ClubElo actually uses, via the explicit mapping above,
    falling back to the FPL name verbatim when no override is listed (true for most clubs)."""
    return _FPL_NAME_TO_CLUBELO_NAME.get(fpl_team_name, fpl_team_name)
