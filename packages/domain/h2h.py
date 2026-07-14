"""H2H league/rival domain enums (Milestone 5 Layer 1: H2H Strategy foundations).

Framework-agnostic (no SQLAlchemy/FastAPI imports here), matching the style
of ``packages/domain/jobs.py``/``packages/domain/rules.py``/``packages/domain/transfers.py``.
"""

from enum import Enum


class LeagueType(str, Enum):
    """Which FPL league family a ``leagues`` row was synced from.

    The FPL API itself already disambiguates this at the endpoint level
    (``entry/{id}/`` returns ``leagues.classic[]``/``leagues.h2h[]`` as
    distinct arrays) — this enum just records which family a given league
    belongs to for our own filtering, it is never inferred by parsing the raw
    ``league_type`` field FPL returns (that field means something different:
    public/private visibility, e.g. ``"s"``/``"x"``, and is preserved
    verbatim in ``raw_league_json`` instead). LEAGUE_SYNC (Layer 1) only ever
    syncs H2H leagues; ``CLASSIC`` exists so the enum surface is stable if a
    later milestone ever syncs classic leagues too.
    """

    CLASSIC = "classic"
    H2H = "h2h"


class LeagueSyncRunStatus(str, Enum):
    """Lifecycle status of a ``league_sync_runs`` row."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    SUCCEEDED_WITH_WARNINGS = "succeeded_with_warnings"
    FAILED = "failed"


class PicksAvailability(str, Enum):
    """The three-way outcome of fetching one rival's picks for one gameweek.

    FPL serves historical picks for ANY manager id publicly once a gameweek
    is finished (confirmed live across ~50 real managers/3 gameweeks) — the
    "private opponent" case could not be reproduced for any finished
    gameweek. What live testing did confirm precisely: a non-existent entry,
    event 0, or a not-yet-happened event all return HTTP 404 with an
    identical body to whatever a genuinely-private response would look like.
    So this system can never confidently distinguish "private" from "not
    released yet" from the HTTP response alone — that ambiguity is modeled
    honestly as one state below, rather than guessing a binary.
    """

    AVAILABLE = "available"
    UNAVAILABLE_PRIVATE_OR_NOT_RELEASED = "unavailable_private_or_not_released"
    # Genuinely different from a clean 404: a timeout, connection error, or
    # non-404 non-2xx response (5xx, 429 exhausted, etc).
    FETCH_FAILED = "fetch_failed"
