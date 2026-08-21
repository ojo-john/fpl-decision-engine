"""Unit tests for app.ingestion.historical_normalization's CSV parsing helpers.

No I/O, no DB — pure parsing/normalization logic against small synthetic
row dicts shaped like real ``gws/merged_gw.csv``/``players_raw.csv`` rows
from different schema eras (see the module docstring for the full
era-by-era column breakdown).
"""

from decimal import Decimal

from app.data_quality import checks
from app.ingestion.historical_normalization import (
    _normalize_pgw_row,
    _normalize_player_row,
    _parse_bool_flag_or_none,
    _parse_bool_or_none,
    _parse_decimal_or_none,
    _parse_int_or_none,
    _parse_int_or_zero,
)

# --- Minimal per-era merged_gw.csv row simulations --------------------------
# Real csv.DictReader rows never contain a key for a column absent from that
# season's header at all - simulated here by simply omitting the key.

_ROW_2020_21_ERA = {
    "element": "100",
    "round": "5",
    "fixture": "42",
    "opponent_team": "7",
    "was_home": "True",
    "position": "MID",
    "minutes": "90",
    "total_points": "6",
    "goals_scored": "1",
    "assists": "0",
    "clean_sheets": "0",
    "goals_conceded": "1",
    "own_goals": "0",
    "penalties_saved": "0",
    "penalties_missed": "0",
    "yellow_cards": "0",
    "red_cards": "0",
    "saves": "0",
    "bonus": "2",
    "bps": "28",
    "ict_index": "8.4",
    "influence": "20.0",
    "creativity": "15.5",
    "threat": "30.0",
    "value": "75",
    "selected": "123456",
    "transfers_in": "1000",
    "transfers_out": "500",
    "transfers_balance": "500",
    # No expected_goals/expected_assists/expected_goal_involvements/
    # expected_goals_conceded/starts/xP columns at all in this era.
}

_ROW_2023_24_ERA = {
    **_ROW_2020_21_ERA,
    "expected_goals": "0.35",
    "expected_assists": "0.12",
    "expected_goal_involvements": "0.47",
    "expected_goals_conceded": "1.10",
    "starts": "1",
    "xP": "4.2",
    # No defensive_contribution/clearances_blocks_interceptions/recoveries/
    # tackles columns at all in this era.
}

_ROW_2025_26_ERA = {
    **_ROW_2023_24_ERA,
    "defensive_contribution": "12",
    "clearances_blocks_interceptions": "9",
    "recoveries": "3",
    "tackles": "2",
}


def test_2020_21_era_row_missing_columns_become_none_not_zero():
    normalized = _normalize_pgw_row(_ROW_2020_21_ERA)
    assert normalized["expected_goals"] is None
    assert normalized["expected_assists"] is None
    assert normalized["expected_goal_involvements"] is None
    assert normalized["expected_goals_conceded"] is None
    assert normalized["starts"] is None
    assert normalized["xp_estimate"] is None
    assert normalized["defensive_contribution"] is None
    assert normalized["clearances_blocks_interceptions"] is None
    assert normalized["recoveries"] is None
    assert normalized["tackles"] is None
    # Always-present scoring counters still parse correctly.
    assert normalized["minutes"] == 90
    assert normalized["total_points"] == 6


def test_2023_24_era_row_has_xg_and_starts_but_not_defensive_contribution():
    normalized = _normalize_pgw_row(_ROW_2023_24_ERA)
    assert normalized["expected_goals"] == Decimal("0.35")
    assert normalized["expected_assists"] == Decimal("0.12")
    assert normalized["expected_goal_involvements"] == Decimal("0.47")
    assert normalized["expected_goals_conceded"] == Decimal("1.10")
    assert normalized["starts"] is True
    assert normalized["xp_estimate"] == Decimal("4.2")
    assert normalized["defensive_contribution"] is None
    assert normalized["clearances_blocks_interceptions"] is None
    assert normalized["recoveries"] is None
    assert normalized["tackles"] is None


def test_2025_26_era_row_has_defensive_contribution_columns():
    normalized = _normalize_pgw_row(_ROW_2025_26_ERA)
    assert normalized["defensive_contribution"] == 12
    assert normalized["clearances_blocks_interceptions"] == 9
    assert normalized["recoveries"] == 3
    assert normalized["tackles"] == 2


def test_blank_column_value_also_becomes_none_not_missing_key():
    row = {**_ROW_2023_24_ERA, "expected_goals": ""}
    normalized = _normalize_pgw_row(row)
    assert normalized["expected_goals"] is None


# --- Real bug (found 2026-08-17): merged_gw.csv's own "position" column uses "GK", not FPL's
# canonical "GKP" -- and does so inconsistently even for the SAME real goalkeeper across
# gameweeks within one season. app.modeling.candidates.KNOWN_POSITIONS only recognizes "GKP", so
# every "GK"-labeled row was silently one-hot-encoded as "unknown position", not goalkeeper. -----


def test_goalkeeper_position_gk_alias_normalizes_to_canonical_gkp():
    row = {**_ROW_2023_24_ERA, "position": "GK"}
    normalized = _normalize_pgw_row(row)
    assert normalized["position"] == "GKP"


def test_goalkeeper_position_already_canonical_gkp_is_unchanged():
    row = {**_ROW_2023_24_ERA, "position": "GKP"}
    normalized = _normalize_pgw_row(row)
    assert normalized["position"] == "GKP"


def test_outfield_positions_are_unaffected_by_the_gk_alias():
    for position in ("DEF", "MID", "FWD"):
        row = {**_ROW_2023_24_ERA, "position": position}
        assert _normalize_pgw_row(row)["position"] == position


def test_manager_pick_position_labels_pass_through_unchanged():
    """ "AM"/"MNG" (2024-25 only) are real Assistant Manager/Manager fantasy picks, not a
    mislabeled player position -- verified against real ingested names (Guardiola, Arteta, ...).
    Correctly excluded from KNOWN_POSITIONS; must NOT be aliased to anything."""
    for position in ("AM", "MNG"):
        row = {**_ROW_2023_24_ERA, "position": position}
        assert _normalize_pgw_row(row)["position"] == position


# --- Raw ownership COUNT vs percent must never be conflated -----------------


def test_selected_count_is_read_as_raw_count_not_percent():
    normalized = _normalize_pgw_row(_ROW_2023_24_ERA)
    assert normalized["selected_by_count_at_time"] == 123456
    assert isinstance(normalized["selected_by_count_at_time"], int)


def test_players_raw_selected_by_percent_is_kept_separate_and_untouched():
    """players_raw.csv's `selected_by_percent` is a wholly different column/file.

    `_normalize_player_row` must leave it as the raw percent-like value
    (parsed downstream via `_parse_decimal`, exactly like live ingestion),
    never populated from/confused with merged_gw's `selected` count.
    """
    player_row = {
        "id": "100",
        "code": "900100",
        "team": "1",
        "element_type": "3",
        "now_cost": "75",
        "selected_by_percent": "12.3",
    }
    normalized = _normalize_player_row(player_row)
    assert normalized["selected_by_percent"] == "12.3"


# --- Blank/null `code` detection ---------------------------------------


def test_normalize_player_row_blank_code_becomes_none():
    row = {"id": "512", "code": "", "team": "18", "element_type": "2", "now_cost": "44"}
    normalized = _normalize_player_row(row)
    assert normalized["code"] is None


def test_check_missing_code_flags_blank_code_as_error():
    rows = [
        {"id": 512, "code": None},
        {"id": 19, "code": 223340},
    ]
    issues = checks.check_missing_code(rows)
    assert len(issues) == 1
    assert issues[0].entity_fpl_id == 512
    assert issues[0].check_name == "missing_code"
    assert issues[0].severity.value == "error"


def test_check_missing_code_does_not_flag_present_code():
    rows = [{"id": 19, "code": 223340}]
    assert checks.check_missing_code(rows) == []


# --- Small parsing helper edge cases -----------------------------------


def test_parse_int_or_zero_blank_and_missing():
    assert _parse_int_or_zero(None) == 0
    assert _parse_int_or_zero("") == 0
    assert _parse_int_or_zero("7") == 7


def test_parse_int_or_none_blank_and_missing():
    assert _parse_int_or_none(None) is None
    assert _parse_int_or_none("") is None
    assert _parse_int_or_none("7") == 7


def test_parse_decimal_or_none_blank_and_missing():
    assert _parse_decimal_or_none(None) is None
    assert _parse_decimal_or_none("") is None
    assert _parse_decimal_or_none("4.4") == Decimal("4.4")


def test_parse_bool_or_none_blank_is_none_not_false():
    assert _parse_bool_or_none(None) is None
    assert _parse_bool_or_none("") is None
    assert _parse_bool_or_none("True") is True
    assert _parse_bool_or_none("False") is False


def test_parse_bool_flag_or_none():
    assert _parse_bool_flag_or_none(None) is None
    assert _parse_bool_flag_or_none("") is None
    assert _parse_bool_flag_or_none("1") is True
    assert _parse_bool_flag_or_none("0") is False
