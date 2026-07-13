"""Unit tests for app.transfers.free_transfers.derive_free_transfers_available."""

import logging

from app.transfers.free_transfers import derive_free_transfers_available

CAP = 5  # 1 + max_extra_free_transfers(4), matching the real 2025-26 rules.


def test_empty_history_defaults_to_one():
    assert derive_free_transfers_available([], [], cap=CAP) == 1


def test_consecutive_zero_transfer_gameweeks_accrue_and_cap():
    # free: 1 -> 2 -> 3 -> 4 -> 5 -> 5 (capped, doesn't exceed CAP)
    rows = [{"event": event, "event_transfers": 0, "event_transfers_cost": 0} for event in range(1, 6)]
    assert derive_free_transfers_available(rows, [], cap=CAP) == 5


def test_wildcard_gameweek_freezes_counter():
    # GW1: 0 transfers -> free goes 1 -> 2.
    # GW2: wildcard played (many transfers, no cost) -> counter frozen at 2, untouched.
    # GW3: 0 transfers -> free goes 2 -> 3 (continuing from the frozen value, not from GW2's transfers).
    rows = [
        {"event": 1, "event_transfers": 0, "event_transfers_cost": 0},
        {"event": 2, "event_transfers": 11, "event_transfers_cost": 0},
        {"event": 3, "event_transfers": 0, "event_transfers_cost": 0},
    ]
    chips = [{"name": "wildcard", "event": 2, "time": "2025-08-22T10:00:00Z"}]
    assert derive_free_transfers_available(rows, chips, cap=CAP) == 3


def test_hit_taking_gameweek_reduces_free_transfers():
    # free starts at 1. GW1: event_transfers=3 > free(1) -> hit of 2 extra transfers.
    # free = max(1-3, 0) = 0, then +1 accrual = 1.
    rows = [{"event": 1, "event_transfers": 3, "event_transfers_cost": -8}]
    assert derive_free_transfers_available(rows, [], cap=CAP) == 1


def test_cross_validation_mismatch_logs_warning_and_does_not_raise(caplog):
    # Actual FPL hit cost for event_transfers=2 with free=1 should be -4, not the
    # -100 fabricated here -- this should log a warning, not raise.
    rows = [{"event": 1, "event_transfers": 2, "event_transfers_cost": -100}]
    with caplog.at_level(logging.WARNING):
        result = derive_free_transfers_available(rows, [], cap=CAP)

    assert result == 1  # max(1-2,0)=0, +1 accrual capped = 1 -- replay still completes normally
    assert any("mismatch" in record.message for record in caplog.records)
