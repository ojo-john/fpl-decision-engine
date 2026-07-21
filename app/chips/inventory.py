"""Pure derivation of the user's own chip inventory against a set of resolved ``ChipWindow``s.

No I/O here -- same "transform already-fetched/already-queried data" style
as ``app.chips.windows``. This is the user-side equivalent of
``app.h2h.rival_state._derive_remaining_chips`` -- both are window-aware
(match an observed play's ``event`` to the specific window it falls in,
rather than treating a chip name as fully spent the moment either window is
used) -- but this module is still deliberately MORE precise: the user's own
``ManagerSquadSnapshot.chips_used_json``/``active_chip`` are authoritative,
exact records of what the user themselves actually did, whereas a rival's
chip-play history (``history_chip_rows``) is a best-effort read of FPL's
public ``/history/`` endpoint with no equivalent of ``active_chip`` -- so
only this module can honestly classify a window as ``ACTIVE_THIS_GAMEWEEK``;
a rival-side equivalent can only ever report ``used``/``available`` (folded
into "remaining", not "expired_unused" as a separate state) for that reason.
"""

from dataclasses import dataclass

from app.chips.windows import ChipWindow
from packages.domain.chips import UserChipWindowStatus


@dataclass(frozen=True)
class UserChipWindow:
    """One ``ChipWindow`` classified against the user's own chip-play history."""

    window: ChipWindow
    status: UserChipWindowStatus
    used_at_fpl_event_id: int | None


def _used_event_for_window(chip_windows_used: list[dict], window: ChipWindow) -> int | None:
    for used in chip_windows_used:
        if used.get("name") != window.chip_type.value:
            continue
        event = used.get("event")
        if event is not None and window.start_event <= event <= window.stop_event:
            return event
    return None


def derive_user_chip_windows(
    chip_windows: list[ChipWindow],
    chips_used_json: list[dict] | None,
    active_chip: str | None,
    current_fpl_event_id: int | None,
) -> list[UserChipWindow]:
    """One ``UserChipWindow`` per input ``ChipWindow``, classified in this precedence order:

    1. ``USED`` -- a ``chips_used_json`` entry's ``name`` matches this window's
       ``chip_type`` AND its ``event`` falls in ``[start_event, stop_event]``.
    2. ``ACTIVE_THIS_GAMEWEEK`` -- ``active_chip`` matches this window's
       ``chip_type`` name AND ``current_fpl_event_id`` falls in
       ``[start_event, stop_event]`` (checked only if not already ``USED``
       above -- an active chip for the CURRENT gameweek won't yet appear in
       ``chips_used_json``'s history, which only records chips from
       gameweeks that have already been played through, so this can never
       double-fire against the same window a ``USED`` entry already covers,
       but the ordering is still checked explicitly for clarity/safety).
    3. ``EXPIRED_UNUSED`` -- ``current_fpl_event_id`` is not ``None`` and
       greater than ``stop_event``, and neither of the above applied.
    4. ``AVAILABLE`` -- none of the above.

    Off-season degenerate case: when ``current_fpl_event_id`` is ``None``
    (no resolvable current gameweek -- see
    ``app.optimization.gameweek_resolution.resolve_target_gameweek``'s own
    ``NO_ACTIVE_GAMEWEEK`` case), ``EXPIRED_UNUSED`` and
    ``ACTIVE_THIS_GAMEWEEK`` can never be produced (both require comparing
    against a known current event) -- only ``USED`` or ``AVAILABLE`` apply.
    """
    chip_windows_used = chips_used_json or []

    results: list[UserChipWindow] = []
    for window in chip_windows:
        used_event = _used_event_for_window(chip_windows_used, window)
        if used_event is not None:
            results.append(UserChipWindow(window, UserChipWindowStatus.USED, used_event))
            continue

        if (
            active_chip == window.chip_type.value
            and current_fpl_event_id is not None
            and window.start_event <= current_fpl_event_id <= window.stop_event
        ):
            results.append(UserChipWindow(window, UserChipWindowStatus.ACTIVE_THIS_GAMEWEEK, None))
            continue

        if current_fpl_event_id is not None and current_fpl_event_id > window.stop_event:
            results.append(UserChipWindow(window, UserChipWindowStatus.EXPIRED_UNUSED, None))
            continue

        results.append(UserChipWindow(window, UserChipWindowStatus.AVAILABLE, None))
    return results
