"""Unit tests for app.ingestion.team_strength's pure parsing/mapping functions. No DB, no network."""

from decimal import Decimal

from app.ingestion.team_strength import (
    parse_ratings_csv,
    premier_league_ratings_by_club,
    resolve_clubelo_name,
)

_REAL_SNAPSHOT_EXCERPT = """Rank,Club,Country,Level,Elo,From,To
1,Arsenal,ENG,1,2063.75805664,2026-05-31,2026-08-21
3,Man City,ENG,1,1970.85388184,2026-05-31,2026-08-23
9,Liverpool,ENG,1,1910.79882812,2026-05-31,2026-08-23
2,Bayern,GER,1,2000.8704834,2026-05-21,2026-08-28
None,Some Lower League Club,ENG,3,1500.0,2026-05-31,2026-08-23
"""


def test_parse_ratings_csv_returns_typed_rows():
    rows = parse_ratings_csv(_REAL_SNAPSHOT_EXCERPT)

    assert len(rows) == 5
    arsenal = next(r for r in rows if r.club == "Arsenal")
    assert arsenal.country == "ENG"
    assert arsenal.level == 1
    assert arsenal.elo == Decimal("2063.75805664")


def test_parse_ratings_csv_skips_malformed_rows_without_raising():
    malformed = (
        "Rank,Club,Country,Level,Elo,From,To\n1,BrokenRow,ENG,1,not-a-number,,\n2,Arsenal,ENG,1,2000,,\n"
    )

    rows = parse_ratings_csv(malformed)

    assert len(rows) == 1
    assert rows[0].club == "Arsenal"


def test_parse_ratings_csv_empty_input_returns_empty_list():
    assert parse_ratings_csv("Rank,Club,Country,Level,Elo,From,To\n") == []


def test_premier_league_ratings_by_club_filters_to_eng_level_1():
    rows = parse_ratings_csv(_REAL_SNAPSHOT_EXCERPT)

    filtered = premier_league_ratings_by_club(rows)

    assert set(filtered) == {"Arsenal", "Man City", "Liverpool"}
    assert filtered["Arsenal"] == Decimal("2063.75805664")


def test_resolve_clubelo_name_maps_known_divergent_names():
    assert resolve_clubelo_name("Man Utd") == "Man United"
    assert resolve_clubelo_name("Spurs") == "Tottenham"
    assert resolve_clubelo_name("Nott'm Forest") == "Forest"
    assert resolve_clubelo_name("Coventry City") == "Coventry"
    assert resolve_clubelo_name("Hull City") == "Hull"
    assert resolve_clubelo_name("Ipswich Town") == "Ipswich"


def test_resolve_clubelo_name_falls_back_to_verbatim_for_unlisted_clubs():
    assert resolve_clubelo_name("Arsenal") == "Arsenal"
    assert resolve_clubelo_name("Brighton") == "Brighton"
