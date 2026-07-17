"""Integration tests for Milestone 7 Layer 0: live per-gameweek stats backfill within DATA_INGESTION.

Requires a running Postgres with migrations applied (see conftest.py).
FPLClient is monkeypatched (see `patched_fpl_client` in conftest.py) so no
real network access happens here — all data comes from the trimmed
tests/fixtures/fpl_api sample files plus small, synthetic ``/live/``-shaped
payloads built below, keyed to bootstrap_static_sample.json's synthetic
players (101-104, on teams 1/2) and fixtures_sample.json's fixture 201
(team 1 vs team 2, gameweek 1, finished).
"""

from sqlalchemy import select

from app.jobs.service import execute_job
from packages.database.models.fixture import Fixture
from packages.database.models.gameweek import Gameweek
from packages.database.models.player import Player
from packages.database.models.player_gameweek_stats import PlayerGameweekStats
from packages.database.models.season import Season
from packages.domain.jobs import JobStatus, JobType, TriggerType

# fixtures_sample.json's fixture 201 is team_h=1 vs team_a=2, event=1, finished.
_FIXTURE_201_FPL_ID = 201


def _element(
    element_id: int,
    fixture: int,
    minutes: int = 90,
    total_points: int = 2,
    goals_scored: int = 0,
    assists: int = 0,
    clean_sheets: int = 0,
    goals_conceded: int = 0,
    bonus: int = 0,
    bps: int = 0,
    saves: int = 0,
    yellow_cards: int = 0,
    red_cards: int = 0,
    defensive_contribution: int = 0,
    starts: int = 1,
    explain_stats: list[dict] | None = None,
) -> dict:
    """Build one real-shaped ``/live/`` `elements[]` entry for a test."""
    stats = {
        "minutes": minutes,
        "goals_scored": goals_scored,
        "assists": assists,
        "clean_sheets": clean_sheets,
        "goals_conceded": goals_conceded,
        "own_goals": 0,
        "penalties_saved": 0,
        "penalties_missed": 0,
        "yellow_cards": yellow_cards,
        "red_cards": red_cards,
        "saves": saves,
        "bonus": bonus,
        "bps": bps,
        "influence": "10.0",
        "creativity": "5.0",
        "threat": "3.0",
        "ict_index": "1.8",
        "clearances_blocks_interceptions": 0,
        "recoveries": 0,
        "tackles": 0,
        "defensive_contribution": defensive_contribution,
        "starts": starts,
        "expected_goals": "0.10",
        "expected_assists": "0.05",
        "expected_goal_involvements": "0.15",
        "expected_goals_conceded": "0.50",
        "total_points": total_points,
        "in_dreamteam": False,
        "played": minutes > 0,
    }
    return {
        "id": element_id,
        "stats": stats,
        "explain": [
            {
                "fixture": fixture,
                "stats": explain_stats
                or [{"identifier": "minutes", "points": 2, "value": minutes, "points_modification": 0}],
            }
        ],
        "modified": False,
    }


def _season_id_for_label(db_session, label: str):
    return db_session.execute(select(Season.id).where(Season.label == label)).scalar_one()


def _gameweek_id_for_event(db_session, season_id, fpl_event_id: int):
    return db_session.execute(
        select(Gameweek.id).where(Gameweek.season_id == season_id, Gameweek.fpl_event_id == fpl_event_id)
    ).scalar_one()


def test_finished_gameweek_backfills_live_stats(db_session, patched_fpl_client):
    patched_fpl_client["live_stats_by_event"][1] = {
        "elements": [
            _element(101, _FIXTURE_201_FPL_ID, minutes=90, total_points=6, saves=2, clean_sheets=1, bps=20),
            _element(102, _FIXTURE_201_FPL_ID, minutes=90, total_points=8, clean_sheets=1, bonus=2, bps=25),
            _element(103, _FIXTURE_201_FPL_ID, minutes=90, total_points=9, goals_scored=1, assists=1, bps=40),
            _element(104, _FIXTURE_201_FPL_ID, minutes=75, total_points=2, bps=5),
        ]
    }

    job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert job.status == JobStatus.SUCCEEDED

    season_id = _season_id_for_label(db_session, job.output_json["season_label"])
    gameweek_1_id = _gameweek_id_for_event(db_session, season_id, 1)

    rows = (
        db_session.execute(
            select(PlayerGameweekStats).where(
                PlayerGameweekStats.season_id == season_id,
                PlayerGameweekStats.gameweek_id == gameweek_1_id,
                PlayerGameweekStats.data_source == "live_event",
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 4

    players_by_code = {
        p.fpl_code: p
        for p in db_session.execute(select(Player).where(Player.season_id == season_id)).scalars().all()
    }
    fixture_201 = db_session.execute(
        select(Fixture).where(Fixture.season_id == season_id, Fixture.fpl_fixture_id == _FIXTURE_201_FPL_ID)
    ).scalar_one()

    rows_by_player_id = {r.player_id: r for r in rows}
    player_101 = players_by_code[900101]
    row_101 = rows_by_player_id[player_101.id]

    assert row_101.total_points == 6
    assert row_101.saves == 2
    assert row_101.clean_sheets == 1
    assert row_101.fixture_id == fixture_201.id
    assert row_101.was_home is True  # team 1 is team_h on fixture 201
    assert row_101.opponent_team_id == fixture_201.away_team_id
    # Sourced from the CURRENT Player row, not fabricated.
    assert row_101.value_at_time == player_101.now_cost
    assert row_101.selected_by_count_at_time is None  # no live equivalent — see module docstring
    assert row_101.xp_estimate is None  # no live equivalent at all
    assert row_101.position_short_name_at_time == "GKP"

    player_103 = players_by_code[900103]
    row_103 = rows_by_player_id[player_103.id]
    assert row_103.was_home is False  # team 2 is team_a on fixture 201
    assert row_103.opponent_team_id == fixture_201.home_team_id
    assert row_103.goals_scored == 1
    assert row_103.assists == 1


def test_rerun_does_not_duplicate_live_stats_rows(db_session, patched_fpl_client):
    patched_fpl_client["live_stats_by_event"][1] = {
        "elements": [_element(101, _FIXTURE_201_FPL_ID, minutes=90, total_points=6)]
    }

    first = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert first.status == JobStatus.SUCCEEDED
    season_id = _season_id_for_label(db_session, first.output_json["season_label"])
    gameweek_1_id = _gameweek_id_for_event(db_session, season_id, 1)

    rows_after_first = (
        db_session.execute(
            select(PlayerGameweekStats).where(
                PlayerGameweekStats.season_id == season_id,
                PlayerGameweekStats.gameweek_id == gameweek_1_id,
                PlayerGameweekStats.data_source == "live_event",
            )
        )
        .scalars()
        .all()
    )
    assert len(rows_after_first) == 1
    row_id_after_first = rows_after_first[0].id

    second = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert second.status == JobStatus.SUCCEEDED

    rows_after_second = (
        db_session.execute(
            select(PlayerGameweekStats).where(
                PlayerGameweekStats.season_id == season_id,
                PlayerGameweekStats.gameweek_id == gameweek_1_id,
                PlayerGameweekStats.data_source == "live_event",
            )
        )
        .scalars()
        .all()
    )
    assert len(rows_after_second) == 1
    assert rows_after_second[0].id == row_id_after_first


def test_unresolvable_element_id_is_dropped_and_logged_not_crashed(db_session, patched_fpl_client):
    patched_fpl_client["live_stats_by_event"][1] = {
        "elements": [
            _element(101, _FIXTURE_201_FPL_ID, minutes=90, total_points=6),
            _element(999999, _FIXTURE_201_FPL_ID, minutes=90, total_points=99),  # no matching Player row
        ]
    }

    job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert job.status == JobStatus.SUCCEEDED  # never crashes the whole run

    season_id = _season_id_for_label(db_session, job.output_json["season_label"])
    gameweek_1_id = _gameweek_id_for_event(db_session, season_id, 1)

    rows = (
        db_session.execute(
            select(PlayerGameweekStats).where(
                PlayerGameweekStats.season_id == season_id,
                PlayerGameweekStats.gameweek_id == gameweek_1_id,
                PlayerGameweekStats.data_source == "live_event",
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1  # only the resolvable player persisted
    assert rows[0].total_points == 6


def test_genuine_double_gameweek_player_gets_two_rows_not_one_doubled_row(db_session, patched_fpl_client):
    # Add a second, synthetic finished fixture in gameweek 1 also involving
    # team 1 (Arsenal) -- a genuine (if artificial) Double Gameweek for
    # player 101.
    second_fixture_fpl_id = 9101
    patched_fpl_client["fixtures"].append(
        {
            "code": 3009101,
            "event": 1,
            "finished": True,
            "finished_provisional": True,
            "id": second_fixture_fpl_id,
            "kickoff_time": "2025-08-18T19:00:00Z",
            "minutes": 90,
            "provisional_start_time": False,
            "pulse_id": 999001,
            "started": True,
            "team_a": 4,
            "team_a_difficulty": 3,
            "team_a_score": 0,
            "team_h": 1,
            "team_h_difficulty": 2,
            "team_h_score": 1,
        }
    )

    patched_fpl_client["live_stats_by_event"][1] = {
        "elements": [
            {
                "id": 101,
                "stats": {
                    "minutes": 180,
                    "goals_scored": 0,
                    "assists": 1,
                    "clean_sheets": 1,
                    "goals_conceded": 0,
                    "own_goals": 0,
                    "penalties_saved": 0,
                    "penalties_missed": 0,
                    "yellow_cards": 0,
                    "red_cards": 0,
                    "saves": 2,
                    "bonus": 3,
                    "bps": 45,
                    "influence": "40.0",
                    "creativity": "10.0",
                    "threat": "0.0",
                    "ict_index": "5.0",
                    "clearances_blocks_interceptions": 0,
                    "recoveries": 0,
                    "tackles": 0,
                    "defensive_contribution": 0,
                    "starts": 2,
                    "expected_goals": "0.00",
                    "expected_assists": "0.10",
                    "expected_goal_involvements": "0.10",
                    "expected_goals_conceded": "0.20",
                    "total_points": 15,  # aggregate: 7 (fixture 201) + 8 (fixture 9101), see explain below
                    "in_dreamteam": False,
                    "played": True,
                },
                "explain": [
                    {
                        "fixture": _FIXTURE_201_FPL_ID,
                        "stats": [
                            {"identifier": "minutes", "points": 2, "value": 90, "points_modification": 0},
                            {"identifier": "clean_sheets", "points": 4, "value": 1, "points_modification": 0},
                            {"identifier": "saves", "points": 1, "value": 2, "points_modification": 0},
                        ],
                    },
                    {
                        "fixture": second_fixture_fpl_id,
                        "stats": [
                            {"identifier": "minutes", "points": 2, "value": 90, "points_modification": 0},
                            {"identifier": "assists", "points": 3, "value": 1, "points_modification": 0},
                            {"identifier": "bonus", "points": 3, "value": 3, "points_modification": 0},
                        ],
                    },
                ],
                "modified": False,
            }
        ]
    }

    job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert job.status == JobStatus.SUCCEEDED

    season_id = _season_id_for_label(db_session, job.output_json["season_label"])
    gameweek_1_id = _gameweek_id_for_event(db_session, season_id, 1)
    player_101 = db_session.execute(
        select(Player).where(Player.season_id == season_id, Player.fpl_code == 900101)
    ).scalar_one()

    rows = (
        db_session.execute(
            select(PlayerGameweekStats).where(
                PlayerGameweekStats.season_id == season_id,
                PlayerGameweekStats.gameweek_id == gameweek_1_id,
                PlayerGameweekStats.player_id == player_101.id,
                PlayerGameweekStats.data_source == "live_event",
            )
        )
        .scalars()
        .all()
    )

    assert len(rows) == 2  # 2 separate rows, never one doubled row
    fixture_fpl_ids = {
        db_session.get(Fixture, r.fixture_id).fpl_fixture_id for r in rows if r.fixture_id is not None
    }
    assert fixture_fpl_ids == {_FIXTURE_201_FPL_ID, second_fixture_fpl_id}

    row_201 = next(
        r for r in rows if db_session.get(Fixture, r.fixture_id).fpl_fixture_id == _FIXTURE_201_FPL_ID
    )
    row_9101 = next(
        r for r in rows if db_session.get(Fixture, r.fixture_id).fpl_fixture_id == second_fixture_fpl_id
    )
    assert row_201.clean_sheets == 1
    assert row_201.saves == 2
    assert row_201.assists == 0
    assert row_9101.assists == 1
    assert row_9101.bonus == 3
    # Never attributable per-fixture from /live/ -- must be NULL, not fabricated.
    assert row_201.ict_index is None
    assert row_9101.ict_index is None
    assert row_201.total_points == 2 + 4 + 1  # minutes + clean_sheets + saves
    assert row_9101.total_points == 2 + 3 + 3  # minutes + assists + bonus
    assert row_201.total_points + row_9101.total_points == 15


def test_multiple_newly_finished_gameweeks_backfilled_in_one_run(db_session, patched_fpl_client):
    patched_fpl_client["live_stats_by_event"][1] = {
        "elements": [_element(101, _FIXTURE_201_FPL_ID, minutes=90, total_points=6)]
    }
    # Gameweek 2 has no committed Fixture rows in this test's fixtures_sample.json,
    # so its fixture reference is legitimately unresolvable — the row still
    # persists with fixture_id=NULL (mirrors historical ingestion's identical
    # nullable-FK convention), which is exactly what's being exercised here:
    # a second, independent gameweek getting backfilled in the same run.
    patched_fpl_client["live_stats_by_event"][2] = {
        "elements": [_element(101, 9999999, minutes=90, total_points=3)]
    }

    job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert job.status in (JobStatus.SUCCEEDED,)

    season_id = _season_id_for_label(db_session, job.output_json["season_label"])
    gameweek_1_id = _gameweek_id_for_event(db_session, season_id, 1)
    gameweek_2_id = _gameweek_id_for_event(db_session, season_id, 2)

    rows_gw1 = (
        db_session.execute(
            select(PlayerGameweekStats).where(
                PlayerGameweekStats.season_id == season_id,
                PlayerGameweekStats.gameweek_id == gameweek_1_id,
                PlayerGameweekStats.data_source == "live_event",
            )
        )
        .scalars()
        .all()
    )
    rows_gw2 = (
        db_session.execute(
            select(PlayerGameweekStats).where(
                PlayerGameweekStats.season_id == season_id,
                PlayerGameweekStats.gameweek_id == gameweek_2_id,
                PlayerGameweekStats.data_source == "live_event",
            )
        )
        .scalars()
        .all()
    )
    assert len(rows_gw1) == 1
    assert len(rows_gw2) == 1
    assert rows_gw2[0].fixture_id is None
    assert rows_gw2[0].total_points == 3
    assert set(job.output_json["live_stats_backfilled_event_ids"]) >= {1, 2}


def test_unfinished_gameweek_is_not_ingested(db_session, patched_fpl_client):
    # Mark gameweek 2 as not yet finished (a realistic in-progress gameweek).
    for event in patched_fpl_client["bootstrap"]["events"]:
        if event["id"] == 2:
            event["finished"] = False

    patched_fpl_client["live_stats_by_event"][2] = {
        "elements": [_element(101, 9999999, minutes=45, total_points=1)]
    }

    job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert job.status == JobStatus.SUCCEEDED

    season_id = _season_id_for_label(db_session, job.output_json["season_label"])
    gameweek_2_id = _gameweek_id_for_event(db_session, season_id, 2)

    rows_gw2 = (
        db_session.execute(
            select(PlayerGameweekStats).where(
                PlayerGameweekStats.season_id == season_id,
                PlayerGameweekStats.gameweek_id == gameweek_2_id,
                PlayerGameweekStats.data_source == "live_event",
            )
        )
        .scalars()
        .all()
    )
    assert len(rows_gw2) == 0
    assert 2 not in job.output_json["live_stats_backfilled_event_ids"]
