"""Chip domain enums (Milestone 6 Layer 1: Chips foundations).

Framework-agnostic (no SQLAlchemy/FastAPI imports here), matching the style
of ``packages/domain/jobs.py``/``packages/domain/rules.py``/``packages/domain/h2h.py``.
"""

from enum import Enum


class ChipType(str, Enum):
    """The closed set of FPL chips, keyed by bootstrap-static's own raw ``chips[].name`` values.

    These 4 exact names (and the fact that there are exactly 4) are hand-
    maintained, necessary plumbing -- the SAME category of gap already
    documented and accepted in ``app.rules.constants.HAND_MAINTAINED_RULES``'s
    ``hand_maintained.chip_effect.*`` entries, not a new kind of hardcoding
    introduced by this module. bootstrap-static's ``chips[]`` array reliably
    reports each chip's *scheduling* (``name``/``start_event``/``stop_event``
    per window -- see ``app.chips.windows.resolve_chip_windows``), but never
    its *mechanics* (what the chip actually DOES) or a canonical enumeration
    of "these are all the chips that exist" -- that has to come from
    knowledge of the real game, same as the transfer-hit-cost and
    captain-multiplier constants already hand-maintained in
    ``HAND_MAINTAINED_RULES``.

    Because this is a closed set derived from real-world knowledge rather
    than from the API's own structure, it is NOT self-updating: if FPL ever
    ships a 5th chip, bootstrap-static will report its name/windows same as
    any other, but nothing in this codebase will recognize it as a
    ``ChipType`` until a human adds it here. Every caller that maps a raw
    chip name to this enum (``app.chips.windows.resolve_chip_windows`` is the
    only such caller today) MUST degrade gracefully on an unrecognized name --
    log a warning and skip that entry, never raise -- so a future 5th chip
    is a silently-incomplete-but-safe degradation, not a crash.
    """

    WILDCARD = "wildcard"
    FREEHIT = "freehit"
    BBOOST = "bboost"
    TRIPLE_CAPTAIN = "3xc"


class UserChipWindowStatus(str, Enum):
    """Classification of one ``ChipWindow`` against the user's own chip-play history.

    See ``app.chips.inventory.derive_user_chip_windows`` for the precedence
    rules used to assign this.
    """

    AVAILABLE = "available"
    USED = "used"
    EXPIRED_UNUSED = "expired_unused"
    ACTIVE_THIS_GAMEWEEK = "active_this_gameweek"


class ChipVerdict(str, Enum):
    """One chip's final scenario-ranking recommendation (Milestone 6 Layer 2: Chip Scenario Ranking).

    See ``app.chips.scenario.rank_candidates_for_chip`` for the exact
    tie-break/materiality-threshold logic that assigns this. Deliberately
    distinct from ``UserChipWindowStatus`` (which classifies a WINDOW against
    play history) -- this classifies the RECOMMENDATION for a chip that is
    still genuinely usable (``AVAILABLE``/``ACTIVE_THIS_GAMEWEEK`` in
    ``UserChipWindowStatus`` terms). ``ALREADY_USED``/``EXPIRED_UNUSED``/
    ``UNAVAILABLE`` exist here too so a caller has a single verdict field to
    report regardless of WHY a chip isn't a live "use now vs hold" decision,
    without needing to cross-reference ``UserChipWindowStatus`` separately.

    ``ChipOptimizationRunStatus`` (persistence lifecycle: running/succeeded/
    failed) is Milestone 6 Layer 3's concern, not this one -- deliberately not
    added yet.
    """

    USE_NOW = "use_now"
    HOLD = "hold"
    NO_VIABLE_WINDOW = "no_viable_window"
    ALREADY_USED = "already_used"
    EXPIRED_UNUSED = "expired_unused"
    UNAVAILABLE = "unavailable"


class ChipOptimizationRunStatus(str, Enum):
    """Lifecycle status of a ``chip_optimization_runs`` row (Milestone 6 Layer 3: job/persistence/REST).

    Structurally identical to ``OptimizationRunStatus``/``TransferOptimizationRunStatus``/
    ``H2HSimulationRunStatus`` (every run-header table in this codebase uses this same
    running/succeeded/failed three-state lifecycle) -- a distinct enum, not a reused one, because
    each is scoped to its own run table by SQLAlchemy convention here (see e.g.
    ``TransferOptimizationRun.run_status``'s own dedicated ``TransferOptimizationRunStatus``).
    """

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
