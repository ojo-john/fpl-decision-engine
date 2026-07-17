"""Unit tests for app.ingestion.live_stats_normalization's pure transformation logic.

Uses the real, trimmed tests/fixtures/fpl_api/event_live_sample.json (fetched
live from the real, public FPL API against the real, already-finished
2025-26 season, event 26 -- a genuine Double Gameweek round).
"""

import json
from decimal import Decimal
from pathlib import Path

from app.ingestion.live_stats_normalization import parse_live_element_to_fixture_rows

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "fpl_api" / "event_live_sample.json"


def _load_elements() -> list[dict]:
    return json.loads(FIXTURE_PATH.read_text())["elements"]


def _element_by_id(elements: list[dict], element_id: int) -> dict:
    return next(e for e in elements if e["id"] == element_id)


def test_single_fixture_element_yields_one_row_with_direct_stats():
    elements = _load_elements()
    element = _element_by_id(elements, 373)  # single fixture, clean sheet + goal, in_dreamteam

    rows = parse_live_element_to_fixture_rows(element)

    assert len(rows) == 1
    row = rows[0]
    assert row["element"] == 373
    assert row["fixture"] == 258
    assert row["minutes"] == 90
    assert row["goals_scored"] == 1
    assert row["clean_sheets"] == 1
    assert row["bonus"] == 3
    assert row["bps"] == 45
    assert row["total_points"] == 17
    assert row["defensive_contribution"] == 13
    assert row["starts"] is True
    assert row["ict_index"] == Decimal("11.0")
    assert row["influence"] == Decimal("61.0")
    assert row["expected_goals"] == Decimal("0.79")
    assert row["extra_stats_json"] == {"in_dreamteam": True, "played": True, "modified": False}


def test_unplayed_element_has_zero_stats_and_starts_false():
    elements = _load_elements()
    element = _element_by_id(elements, 12)

    rows = parse_live_element_to_fixture_rows(element)

    assert len(rows) == 1
    row = rows[0]
    assert row["fixture"] == 257
    assert row["minutes"] == 0
    assert row["total_points"] == 0
    assert row["starts"] is False
    assert row["extra_stats_json"] == {"in_dreamteam": False, "played": False, "modified": False}


def test_red_card_and_negative_points_round_trip():
    elements = _load_elements()
    element = _element_by_id(elements, 293)

    rows = parse_live_element_to_fixture_rows(element)

    assert len(rows) == 1
    row = rows[0]
    assert row["red_cards"] == 1
    assert row["total_points"] == -2
    assert row["bps"] == -12


def test_goalkeeper_saves_and_clean_sheet():
    elements = _load_elements()
    element = _element_by_id(elements, 32)

    rows = parse_live_element_to_fixture_rows(element)

    assert len(rows) == 1
    row = rows[0]
    assert row["saves"] == 3
    assert row["clean_sheets"] == 1
    assert row["yellow_cards"] == 1


def test_double_gameweek_element_yields_two_rows_not_one_doubled_row():
    elements = _load_elements()
    element = _element_by_id(elements, 725)  # real DGW: 2 fixtures, 170 combined minutes

    rows = parse_live_element_to_fixture_rows(element)

    assert len(rows) == 2
    fixtures = {row["fixture"] for row in rows}
    assert fixtures == {252, 310}

    row_252 = next(r for r in rows if r["fixture"] == 252)
    row_310 = next(r for r in rows if r["fixture"] == 310)

    # Per-fixture minutes/points reconstructed from `explain`, not the
    # gameweek-aggregate `stats` object (which would double-count).
    assert row_252["minutes"] == 80
    assert row_310["minutes"] == 90
    assert row_252["assists"] == 1
    assert row_310["goals_scored"] == 1
    assert row_310["goals_conceded"] == 2

    # Per-fixture total_points sums back to the top-level aggregate.
    assert row_252["total_points"] + row_310["total_points"] == element["stats"]["total_points"]
    assert row_252["total_points"] == 2 + 3 + 2  # minutes + assists + bonus
    assert row_310["total_points"] == 2 + 6 - 1 + 1  # minutes + goals - goals_conceded + bonus


def test_double_gameweek_split_rows_null_out_unattributable_aggregate_fields():
    elements = _load_elements()
    element = _element_by_id(elements, 5)  # real DGW: yellow card in one leg, assist in the other

    rows = parse_live_element_to_fixture_rows(element)

    assert len(rows) == 2
    for row in rows:
        # Never attributable per-fixture from the live endpoint -- must be
        # NULL (nullable columns), never a fabricated split of the aggregate.
        assert row["ict_index"] is None
        assert row["influence"] is None
        assert row["creativity"] is None
        assert row["threat"] is None
        assert row["expected_goals"] is None
        assert row["expected_assists"] is None
        assert row["expected_goal_involvements"] is None
        assert row["expected_goals_conceded"] is None
        assert row["starts"] is None
        assert row["clearances_blocks_interceptions"] is None
        assert row["recoveries"] is None
        assert row["tackles"] is None
        # bps is NOT NULL on the schema -- 0 is the documented, honest
        # least-wrong default for an unattributable-per-fixture split row.
        assert row["bps"] == 0
        assert row["extra_stats_json"]["double_gameweek_split"] is True

    row_252 = next(r for r in rows if r["fixture"] == 252)
    row_310 = next(r for r in rows if r["fixture"] == 310)
    assert row_252["yellow_cards"] == 1
    assert row_252["defensive_contribution"] == 18
    assert row_310["assists"] == 1
    assert row_310["goals_conceded"] == 2


def test_element_with_no_explain_entries_defensively_yields_one_null_fixture_row():
    element = {
        "id": 999,
        "stats": {"minutes": 0, "total_points": 0, "played": False},
        "explain": [],
        "modified": False,
    }

    rows = parse_live_element_to_fixture_rows(element)

    assert len(rows) == 1
    assert rows[0]["fixture"] is None
    assert rows[0]["element"] == 999
