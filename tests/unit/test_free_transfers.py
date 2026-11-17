"""Unit tests for app.transfers.free_transfers.derive_free_transfers_available."""

import logging

from app.transfers.free_transfers import derive_free_transfers_available

CAP = 5  # 1 + max_extra_free_transfers(4), matching the real 2025-26 rules.


def _row(event: int, transfers: int = 0, cost: int = 0) -> dict:
    return {"event": event, "event_transfers": transfers, "event_transfers_cost": cost}


def test_derive_emptyHistory_returnsOne():
    assert derive_free_transfers_available([], [], cap=CAP) == 1


def test_derive_onlySquadSelectionGameweek_returnsOne():
    # GW1 is squad selection: GW2 starts with exactly 1 free transfer, not 2.
    assert derive_free_transfers_available([_row(1)], [], cap=CAP) == 1


def test_derive_midSeasonJoiner_firstRowIsSquadSelection():
    # Joined at GW5: GW5 is their squad selection, GW6 has 1, GW7 has 2.
    assert derive_free_transfers_available([_row(5), _row(6)], [], cap=CAP) == 2


def test_derive_consecutiveZeroTransferGameweeks_accrueAndCap():
    # After GW1..GW8 with no transfers: GW2=1, GW3=2, GW4=3, GW5=4, GW6..GW9=5 (capped).
    rows = [_row(event) for event in range(1, 9)]
    assert derive_free_transfers_available(rows, [], cap=CAP) == 5


def test_derive_beforeCapReached_isNotInflatedByOne():
    # After GW1..GW3 with no transfers, GW4 has 3 (GW2=1, GW3=2, GW4=3) -- not 4.
    rows = [_row(event) for event in range(1, 4)]
    assert derive_free_transfers_available(rows, [], cap=CAP) == 3


def test_derive_wildcardGameweek_freezesCounter():
    # GW2 has 1. GW2 wildcard: frozen at 1. GW3 has 1, unused -> GW4 has 2.
    rows = [_row(1), _row(2, transfers=11), _row(3)]
    chips = [{"name": "wildcard", "event": 2, "time": "2025-08-22T10:00:00Z"}]
    assert derive_free_transfers_available(rows, chips, cap=CAP) == 2


def test_derive_realManager918600History_matchesFplAppCountOfTwo():
    # Real GW1-10 history for manager 918600 (2026-27), checked against the
    # FPL app's own count of 2 free transfers for GW11.
    rows = [
        _row(1),
        _row(2, transfers=1),
        _row(3),
        _row(4),
        _row(5),
        _row(6),
        _row(7, transfers=4),
        _row(8),
        _row(9, transfers=2),
        _row(10),
    ]
    chips = [
        {"name": "wildcard", "event": 3, "time": "2026-09-12T10:00:00Z"},
        {"name": "3xc", "event": 7, "time": "2026-10-10T10:00:00Z"},
    ]
    assert derive_free_transfers_available(rows, chips, cap=CAP) == 2


def test_derive_hitTakingGameweek_reducesFreeTransfers():
    # GW2 has 1; 3 transfers is a 2-transfer hit (-8). Left with 0, GW3 has 1.
    rows = [_row(1), _row(2, transfers=3, cost=-8)]
    assert derive_free_transfers_available(rows, [], cap=CAP) == 1


def test_derive_hitCostMismatch_logsWarningAndDoesNotRaise(caplog):
    # GW2: 2 transfers with 1 free should cost -4, not the fabricated -100.
    rows = [_row(1), _row(2, transfers=2, cost=-100)]
    with caplog.at_level(logging.WARNING):
        result = derive_free_transfers_available(rows, [], cap=CAP)

    assert (result, any("mismatch" in r.message for r in caplog.records)) == (1, True)
