"""Integration tests for the HISTORICAL_DATA_INGESTION job end-to-end through execute_job.

Requires a running Postgres with migrations applied (see conftest.py).
``HistoricalFPLClient`` is monkeypatched (``patched_historical_fpl_client`` in
conftest.py) so no real network access happens here — all data comes from
the committed ``tests/fixtures/historical/2023-24/`` CSV files (real,
trimmed samples fetched from the actual live GitHub source).
"""

from sqlalchemy import select

from app.jobs.service import execute_job
from packages.database.models.fixture import Fixture
from packages.database.models.gameweek import Gameweek
from packages.database.models.player import Player
from packages.database.models.player_gameweek_stats import PlayerGameweekStats
from packages.database.models.position import Position
from packages.database.models.season import Season
from packages.database.models.team import Team
from packages.domain.jobs import JobStatus, JobType, TriggerType


def _season(db_session, label: str) -> Season:
    return db_session.execute(select(Season).where(Season.label == label)).scalar_one()


def _run_job(db_session):
    return execute_job(
        db_session,
        job_type=JobType.HISTORICAL_DATA_INGESTION,
        parameters={"season_label": "2023-24"},
        trigger_type=TriggerType.LOCAL,
    )


def test_historical_data_ingestion_row_counts_and_quarantine(db_session, patched_historical_fpl_client):
    job = _run_job(db_session)

    assert job.status == JobStatus.SUCCEEDED
    output = job.output_json
    assert output["season_labels"] == ["2023-24"]
    season_result = output["season_results"]["2023-24"]
    assert season_result["status"] in ("succeeded", "succeeded_with_warnings")
    assert season_result["season_label"] == "2023-24"

    season = _season(db_session, "2023-24")
    assert season.is_current is False

    positions = db_session.execute(select(Position).where(Position.season_id == season.id)).scalars().all()
    assert len(positions) == 5  # GKP/DEF/MID/FWD + the hardcoded Manager (MNG) placeholder

    teams = db_session.execute(select(Team).where(Team.season_id == season.id)).scalars().all()
    assert len(teams) == 20

    players = db_session.execute(select(Player).where(Player.season_id == season.id)).scalars().all()
    assert len(players) == 18

    players_by_element = {p.fpl_element_id: p for p in players}
    quarantined_player = players_by_element[512]
    assert quarantined_player.is_quarantined is True
    assert quarantined_player.fpl_code is None
    assert quarantined_player.quarantine_reason is not None
    assert "missing_code" in quarantined_player.quarantine_reason

    clean_player_count = sum(1 for p in players if not p.is_quarantined)
    assert clean_player_count == 17

    gameweeks = db_session.execute(select(Gameweek).where(Gameweek.season_id == season.id)).scalars().all()
    assert len(gameweeks) == 4
    for gw in gameweeks:
        assert gw.finished is True
        assert gw.data_checked is True
        assert gw.is_current is False

    fixtures = db_session.execute(select(Fixture).where(Fixture.season_id == season.id)).scalars().all()
    assert len(fixtures) == 24
    assert all(not f.is_quarantined for f in fixtures)

    pgw_rows = (
        db_session.execute(select(PlayerGameweekStats).where(PlayerGameweekStats.season_id == season.id))
        .scalars()
        .all()
    )
    assert len(pgw_rows) == 72
    assert all(not row.is_quarantined for row in pgw_rows)

    # At least one player_gameweek_stats row correctly resolves player_id via
    # element -> id -> code to the right Player.id (Phil Foden, element 353).
    foden = players_by_element[353]
    foden_rows = [row for row in pgw_rows if row.player_id == foden.id]
    assert len(foden_rows) == 4  # rounds 1-4
    gw1_row = next(row for row in foden_rows if row.total_points == 3 and row.minutes == 90)
    assert gw1_row.assists == 0
    assert gw1_row.position_short_name_at_time == "MID"
    assert gw1_row.selected_by_count_at_time == 706434
    assert gw1_row.value_at_time == 75


def test_historical_data_ingestion_is_idempotent(db_session, patched_historical_fpl_client):
    first = _run_job(db_session)
    assert first.status == JobStatus.SUCCEEDED

    second = _run_job(db_session)
    assert second.status == JobStatus.SUCCEEDED

    season = _season(db_session, "2023-24")

    teams = db_session.execute(select(Team).where(Team.season_id == season.id)).scalars().all()
    players = db_session.execute(select(Player).where(Player.season_id == season.id)).scalars().all()
    gameweeks = db_session.execute(select(Gameweek).where(Gameweek.season_id == season.id)).scalars().all()
    fixtures = db_session.execute(select(Fixture).where(Fixture.season_id == season.id)).scalars().all()
    pgw_rows = (
        db_session.execute(select(PlayerGameweekStats).where(PlayerGameweekStats.season_id == season.id))
        .scalars()
        .all()
    )

    assert len(teams) == 20
    assert len(players) == 18
    assert len(gameweeks) == 4
    assert len(fixtures) == 24
    assert len(pgw_rows) == 72

    # Re-running only ever produced one Season row for this label.
    seasons = db_session.execute(select(Season).where(Season.label == "2023-24")).scalars().all()
    assert len(seasons) == 1
