"""Pure derivation of a manager's current free-transfer count via forward replay.

No FPL endpoint reports "free transfers remaining" directly — this module
reconstructs it by forward-replaying ``history().current[]`` (one row per
completed gameweek) combined with ``history().chips[]`` (which gameweeks, if
any, played a wildcard/free-hit).

Real FPL mechanics, in the exact order applied per gameweek:
1. The free transfers carried into this gameweek are capped at ``cap``
   (``1 + rules.max_extra_free_transfers``, 5 under 2025-26 rules).
2. Some number of transfers (``event_transfers``) are spent. Any spent beyond
   the capped available count cost ``-4`` points each (a "hit") — this
   module doesn't return that cost, but cross-validates its own implied
   formula against the observed ``event_transfers_cost`` for each gameweek,
   as a sanity check on the whole replay.
3. The remaining count (after spending) accrues by 1 for the next gameweek,
   again capped at ``cap``.

A wildcard/free-hit gameweek is a special case: it grants unlimited free
transfers for that gameweek only, and — critically — neither consumes nor
accrues the ongoing free-transfer counter. That gameweek is skipped entirely
in the replay; the counter carries over unchanged into the following one.
"""

import logging

logger = logging.getLogger(__name__)

_HIT_COST_PER_TRANSFER = 4
_CHIP_NAMES_FREEZING_TRANSFERS = ("wildcard", "freehit")


def derive_free_transfers_available(current_rows: list[dict], chip_rows: list[dict], cap: int) -> int:
    """Forward-replay ``history().current[]``/``chips[]`` to derive free transfers available now.

    ``cap`` is ``1 + rules.max_extra_free_transfers`` (5 today). Defaults to
    1 (the fresh-season baseline) if ``current_rows`` is empty. Never
    raises: a cross-validation mismatch between this replay's implied hit
    cost and the observed ``event_transfers_cost`` for a gameweek is logged
    as a warning only — an unmodeled edge case (e.g. an FPL rule change, or a
    chip this replay doesn't yet account for) should degrade gracefully, not
    crash the sync.
    """
    chip_events = {c["event"] for c in chip_rows if c["name"] in _CHIP_NAMES_FREEZING_TRANSFERS}

    free = 1
    for row in sorted(current_rows, key=lambda r: r["event"]):
        if row["event"] in chip_events:
            # Wildcard/free-hit: unlimited free transfers this gameweek only;
            # the ongoing counter is frozen (neither consumed nor accrued).
            continue

        free = min(free, cap)
        event_transfers = row["event_transfers"]
        expected_cost = -_HIT_COST_PER_TRANSFER * max(0, event_transfers - free)
        observed_cost = row.get("event_transfers_cost", expected_cost)
        if expected_cost != observed_cost:
            logger.warning(
                "free-transfer replay mismatch at event=%s: expected hit cost %s "
                "(free=%s, event_transfers=%s) but observed event_transfers_cost=%s",
                row.get("event"),
                expected_cost,
                free,
                event_transfers,
                observed_cost,
            )

        free = max(free - event_transfers, 0)
        free = min(free + 1, cap)

    return free
