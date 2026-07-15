"""Pure resolution of chip scheduling windows from an already-fetched bootstrap-static payload.

No I/O here -- mirrors ``app.rules.extraction``'s "transform an
already-fetched payload" discipline exactly (in fact this module resolves
the SAME ``bootstrap_static.chips[]`` array that
``app.rules.extraction._flatten_chips`` flattens into ``RuleField``s; this
module instead produces a small, typed, chip-aware structure for
Milestone 6's chip-recommendation logic to consume).

``window_number`` cannot be read off any ``number`` field in the raw
payload: confirmed by inspecting the real, committed
``tests/fixtures/fpl_api/bootstrap_static_sample.json``, every entry's
``number`` field is hardcoded to ``1`` regardless of which of a chip's two
windows it actually is -- FPL does not use this field the way its name
suggests. ``window_number`` is instead computed POSITIONALLY: the Nth time
a given chip name appears in the input list, in list order, is that chip's
Nth window (1 or 2) -- the exact same ``window_counters`` approach
``app.rules.extraction._flatten_chips`` already uses for its own
``window_N`` path segments, applied here for the same reason.
"""

import logging
from dataclasses import dataclass
from typing import Any

from packages.domain.chips import ChipType

logger = logging.getLogger(__name__)

_CHIP_TYPES_BY_NAME: dict[str, ChipType] = {chip_type.value: chip_type for chip_type in ChipType}


@dataclass(frozen=True)
class ChipWindow:
    """One scheduling window (of a chip's typically-two-per-season windows)."""

    chip_type: ChipType
    window_number: int  # 1 or 2, computed positionally -- see module docstring
    start_event: int
    stop_event: int


def resolve_chip_windows(raw_chips_json: list[dict[str, Any]]) -> list[ChipWindow]:
    """One ``ChipWindow`` per ``raw_chips_json`` entry whose ``name`` maps to a known ``ChipType``.

    Entries with an unrecognized name are skipped (logged as a warning), never
    raised -- see ``ChipType``'s own docstring for why an unrecognized chip
    name must degrade gracefully rather than crash (e.g. bootstrap-static
    ever adding a 5th chip before this codebase's hand-maintained enum is
    updated to recognize it).
    """
    window_counters: dict[str, int] = {}
    windows: list[ChipWindow] = []
    for chip in raw_chips_json:
        name = chip.get("name")
        if not isinstance(name, str):
            logger.warning("resolve_chip_windows: skipping chip entry with missing/invalid name: %r", chip)
            continue

        window_counters[name] = window_counters.get(name, 0) + 1
        window_number = window_counters[name]

        chip_type = _CHIP_TYPES_BY_NAME.get(name)
        if chip_type is None:
            logger.warning(
                "resolve_chip_windows: skipping unrecognized chip name %r (window %d) -- "
                "not in ChipType; bootstrap-static may have added a new chip",
                name,
                window_number,
            )
            continue

        windows.append(
            ChipWindow(
                chip_type=chip_type,
                window_number=window_number,
                start_event=chip["start_event"],
                stop_event=chip["stop_event"],
            )
        )
    return windows
