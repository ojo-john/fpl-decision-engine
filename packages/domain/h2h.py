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


class RivalStateConfidence(str, Enum):
    """How confident Layer 2's derived/predicted ``rival_profiles`` fields are, given the evidence so far.

    Deliberately a distinct enum from ``packages.domain.rules.RuleConfidence``
    (explicit Milestone 5 plan decision, not an oversight) — ``RuleConfidence``
    means "how authoritative is this rule VALUE" (confirmed-by-the-API vs.
    hand-maintained-guess); this means something else entirely: "how much
    evidence backs this STATISTICAL ESTIMATE about a rival's behavior". A
    rule's confidence never changes based on sample size; a rival's does.

    ``overall_confidence`` on a ``RivalProfile`` row is always the honest
    floor across every sub-computation (see ``app.h2h.rival_state``) — e.g. a
    rival profiled from a single observed gameweek snapshot gets ``LOW``
    overall even if every other input looks clean, because a single
    observation is never evidence of a stable tendency.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class H2HSimulationRunStatus(str, Enum):
    """Lifecycle status of an ``h2h_simulation_runs`` row (Milestone 5 Layer 3)."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class H2HStrategyLabel(str, Enum):
    """The 7 named risk-adjusted strategy candidates compared for one H2H matchup (Milestone 5 Layer 4).

    Declaration order below is also this system's own "least changed from
    EP_OPTIMAL" ordering, reused as the final, fully-deterministic tie-break
    step when two candidates land on an identical win probability (see
    ``app.h2h.strategy.select_recommended_candidate``'s own docstring for the
    full tie-break rule).
    """

    EP_OPTIMAL = "ep_optimal"
    LOW_VARIANCE = "low_variance"
    HIGH_UPSIDE = "high_upside"
    CAPTAIN_COVER = "captain_cover"
    DIFFERENTIAL_CAPTAIN = "differential_captain"
    NO_HIT = "no_hit"
    CALCULATED_HIT = "calculated_hit"
