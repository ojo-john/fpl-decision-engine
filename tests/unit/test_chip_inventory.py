"""Unit tests for app.chips.inventory.derive_user_chip_windows (pure, no DB/network).

Pins down every one of the 4 classification outcomes, the precedence between
them, and the off-season (current_fpl_event_id=None) degenerate case.
"""

from app.chips.inventory import UserChipWindow, derive_user_chip_windows
from app.chips.windows import ChipWindow
from packages.domain.chips import ChipType, UserChipWindowStatus

_WILDCARD_1 = ChipWindow(ChipType.WILDCARD, 1, 2, 19)
_WILDCARD_2 = ChipWindow(ChipType.WILDCARD, 2, 20, 38)
_BBOOST_1 = ChipWindow(ChipType.BBOOST, 1, 1, 19)


# --- USED ---------------------------------------------------------------


def test_used_when_chips_used_json_entry_falls_within_window():
    result = derive_user_chip_windows(
        chip_windows=[_WILDCARD_1],
        chips_used_json=[{"name": "wildcard", "event": 5, "time": "2025-09-01T00:00:00Z"}],
        active_chip=None,
        current_fpl_event_id=10,
    )

    assert result == [UserChipWindow(_WILDCARD_1, UserChipWindowStatus.USED, 5)]


def test_used_entry_outside_window_boundaries_does_not_mark_used():
    # Played in window 2 (event 25) -- must not mark window 1 (events 2-19) as used.
    result = derive_user_chip_windows(
        chip_windows=[_WILDCARD_1, _WILDCARD_2],
        chips_used_json=[{"name": "wildcard", "event": 25, "time": "x"}],
        active_chip=None,
        current_fpl_event_id=30,
    )

    by_window = {uc.window: uc for uc in result}
    assert by_window[_WILDCARD_1].status == UserChipWindowStatus.EXPIRED_UNUSED
    assert by_window[_WILDCARD_2].status == UserChipWindowStatus.USED
    assert by_window[_WILDCARD_2].used_at_fpl_event_id == 25


# --- ACTIVE_THIS_GAMEWEEK -------------------------------------------------


def test_active_this_gameweek_when_active_chip_matches_and_current_event_in_window():
    result = derive_user_chip_windows(
        chip_windows=[_WILDCARD_1],
        chips_used_json=None,
        active_chip="wildcard",
        current_fpl_event_id=10,
    )

    assert result == [UserChipWindow(_WILDCARD_1, UserChipWindowStatus.ACTIVE_THIS_GAMEWEEK, None)]


def test_active_chip_matching_name_but_current_event_outside_window_is_not_active():
    # active_chip="wildcard" but current event is in the *other* window's range.
    result = derive_user_chip_windows(
        chip_windows=[_WILDCARD_1],
        chips_used_json=None,
        active_chip="wildcard",
        current_fpl_event_id=25,
    )

    assert result == [UserChipWindow(_WILDCARD_1, UserChipWindowStatus.EXPIRED_UNUSED, None)]


def test_used_takes_precedence_over_active_chip_for_the_same_window():
    # Defensive case: chips_used_json already records this window as played,
    # even though active_chip also still names it -- USED must win.
    result = derive_user_chip_windows(
        chip_windows=[_WILDCARD_1],
        chips_used_json=[{"name": "wildcard", "event": 5, "time": "x"}],
        active_chip="wildcard",
        current_fpl_event_id=5,
    )

    assert result == [UserChipWindow(_WILDCARD_1, UserChipWindowStatus.USED, 5)]


# --- EXPIRED_UNUSED --------------------------------------------------------


def test_expired_unused_when_current_event_past_stop_event_and_never_used():
    result = derive_user_chip_windows(
        chip_windows=[_WILDCARD_1],
        chips_used_json=None,
        active_chip=None,
        current_fpl_event_id=20,
    )

    assert result == [UserChipWindow(_WILDCARD_1, UserChipWindowStatus.EXPIRED_UNUSED, None)]


def test_expired_unused_boundary_current_event_equal_to_stop_event_is_not_expired():
    # stop_event itself is still IN the window (inclusive) -- not yet expired.
    result = derive_user_chip_windows(
        chip_windows=[_WILDCARD_1],
        chips_used_json=None,
        active_chip=None,
        current_fpl_event_id=19,
    )

    assert result == [UserChipWindow(_WILDCARD_1, UserChipWindowStatus.AVAILABLE, None)]


# --- AVAILABLE -------------------------------------------------------------


def test_available_when_current_event_within_window_and_not_used_or_active():
    result = derive_user_chip_windows(
        chip_windows=[_BBOOST_1],
        chips_used_json=None,
        active_chip=None,
        current_fpl_event_id=5,
    )

    assert result == [UserChipWindow(_BBOOST_1, UserChipWindowStatus.AVAILABLE, None)]


def test_available_when_current_event_before_window_starts():
    result = derive_user_chip_windows(
        chip_windows=[_WILDCARD_1],  # starts at event 2
        chips_used_json=None,
        active_chip=None,
        current_fpl_event_id=1,
    )

    assert result == [UserChipWindow(_WILDCARD_1, UserChipWindowStatus.AVAILABLE, None)]


# --- Off-season degenerate case: current_fpl_event_id=None -----------------


def test_off_season_never_produces_expired_unused():
    result = derive_user_chip_windows(
        chip_windows=[_WILDCARD_1, _WILDCARD_2, _BBOOST_1],
        chips_used_json=None,
        active_chip=None,
        current_fpl_event_id=None,
    )

    assert all(uc.status != UserChipWindowStatus.EXPIRED_UNUSED for uc in result)
    assert all(uc.status == UserChipWindowStatus.AVAILABLE for uc in result)


def test_off_season_never_produces_active_this_gameweek_even_with_active_chip_set():
    # active_chip alone can't produce ACTIVE_THIS_GAMEWEEK without a known current event.
    result = derive_user_chip_windows(
        chip_windows=[_WILDCARD_1],
        chips_used_json=None,
        active_chip="wildcard",
        current_fpl_event_id=None,
    )

    assert result == [UserChipWindow(_WILDCARD_1, UserChipWindowStatus.AVAILABLE, None)]


def test_off_season_still_reports_used_from_authoritative_history():
    result = derive_user_chip_windows(
        chip_windows=[_WILDCARD_1],
        chips_used_json=[{"name": "wildcard", "event": 5, "time": "x"}],
        active_chip=None,
        current_fpl_event_id=None,
    )

    assert result == [UserChipWindow(_WILDCARD_1, UserChipWindowStatus.USED, 5)]


# --- Multiple windows, mixed outcomes in one call --------------------------


def test_multiple_windows_classified_independently_in_one_call():
    # Window 1 played (event 5, within 2-19); window 2 (20-38) never played
    # and the season has since moved past it entirely (event 39).
    result = derive_user_chip_windows(
        chip_windows=[_WILDCARD_1, _WILDCARD_2],
        chips_used_json=[{"name": "wildcard", "event": 5, "time": "x"}],
        active_chip=None,
        current_fpl_event_id=39,
    )

    by_window = {uc.window: uc.status for uc in result}
    assert by_window[_WILDCARD_1] == UserChipWindowStatus.USED
    assert by_window[_WILDCARD_2] == UserChipWindowStatus.EXPIRED_UNUSED
