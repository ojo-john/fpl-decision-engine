"""Unit tests for app.chips.windows.resolve_chip_windows (pure, no DB/network).

Uses the REAL committed tests/fixtures/fpl_api/bootstrap_static_sample.json's
own chips[] array (not a hand-built fixture) so this test is pinned to the
actual, live shape of bootstrap-static's chips[] -- including the
confirmed fact that its `number` field is hardcoded to 1 for every entry
regardless of which window it is, which is exactly why window_number is
computed positionally rather than trusted from the payload.
"""

import json
from pathlib import Path

from app.chips.windows import ChipWindow, resolve_chip_windows
from packages.domain.chips import ChipType

_FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "fpl_api" / "bootstrap_static_sample.json"


def _load_real_chips() -> list[dict]:
    return json.loads(_FIXTURE_PATH.read_text())["chips"]


def test_resolve_chip_windows_against_real_fixture_yields_two_windows_per_chip_type():
    windows = resolve_chip_windows(_load_real_chips())

    by_type: dict[ChipType, list[ChipWindow]] = {}
    for window in windows:
        by_type.setdefault(window.chip_type, []).append(window)

    assert set(by_type.keys()) == set(ChipType)
    for chip_type, chip_windows in by_type.items():
        assert len(chip_windows) == 2, f"expected exactly 2 windows for {chip_type}"
        assert sorted(w.window_number for w in chip_windows) == [1, 2]


def test_resolve_chip_windows_real_fixture_start_stop_events_match_committed_fixture():
    windows = resolve_chip_windows(_load_real_chips())
    by_key = {(w.chip_type, w.window_number): w for w in windows}

    # Pinned directly against the real chips[] array committed in
    # tests/fixtures/fpl_api/bootstrap_static_sample.json (read verbatim
    # above, not guessed) -- if that fixture is ever regenerated with
    # different boundaries, this test should be updated to match.
    assert by_key[(ChipType.WILDCARD, 1)] == ChipWindow(ChipType.WILDCARD, 1, 2, 19)
    assert by_key[(ChipType.WILDCARD, 2)] == ChipWindow(ChipType.WILDCARD, 2, 20, 38)
    assert by_key[(ChipType.FREEHIT, 1)] == ChipWindow(ChipType.FREEHIT, 1, 2, 19)
    assert by_key[(ChipType.FREEHIT, 2)] == ChipWindow(ChipType.FREEHIT, 2, 20, 38)
    assert by_key[(ChipType.BBOOST, 1)] == ChipWindow(ChipType.BBOOST, 1, 1, 19)
    assert by_key[(ChipType.BBOOST, 2)] == ChipWindow(ChipType.BBOOST, 2, 20, 38)
    assert by_key[(ChipType.TRIPLE_CAPTAIN, 1)] == ChipWindow(ChipType.TRIPLE_CAPTAIN, 1, 1, 19)
    assert by_key[(ChipType.TRIPLE_CAPTAIN, 2)] == ChipWindow(ChipType.TRIPLE_CAPTAIN, 2, 20, 38)


def test_resolve_chip_windows_real_fixture_number_field_is_not_trustworthy():
    """Pin down the actual finding that motivated positional window_number computation:
    every real chips[] entry's `number` field is hardcoded to 1, so it cannot be used
    to distinguish a chip's first window from its second.
    """
    raw_chips = _load_real_chips()
    assert all(chip["number"] == 1 for chip in raw_chips)


def test_resolve_chip_windows_skips_unrecognized_chip_name_gracefully():
    raw_chips = _load_real_chips() + [
        {
            "id": 99,
            "name": "some_future_chip",
            "number": 1,
            "start_event": 1,
            "stop_event": 38,
            "chip_type": "team",
        }
    ]

    windows = resolve_chip_windows(raw_chips)

    assert all(w.chip_type != "some_future_chip" for w in windows)
    assert len(windows) == 8  # only the 4 real chip types x 2 windows each, the bad entry skipped
