"""Integration tests for the DATA_INGESTION job end-to-end through execute_job.

Requires a running Postgres with migrations applied (see conftest.py).
FPLClient is monkeypatched (see `patched_fpl_client` in conftest.py) so no
real network access happens here — all data comes from the trimmed
tests/fixtures/fpl_api sample files.
"""

import uuid
from decimal import Decimal

from sqlalchemy import select

from app.jobs.service import execute_job
from packages.database.models.data_quality_issue import DataQualityIssue
from packages.database.models.fixture import Fixture
from packages.database.models.gameweek import Gameweek
from packages.database.models.ingestion_run import IngestionRun
from packages.database.models.player import Player
from packages.database.models.player_price_signal_observation import PlayerPriceSignalObservation
from packages.database.models.position import Position
from packages.database.models.season import Season
from packages.database.models.team import Team
from packages.domain.ingestion import DataQualitySeverity, IngestionRunStatus
from packages.domain.jobs import JobStatus, JobType, TriggerType


def _season_id_for_label(db_session, label: str):
    return db_session.execute(select(Season.id).where(Season.label == label)).scalar_one()


def test_data_ingestion_row_counts_and_quarantine(db_session, patched_fpl_client):
    job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)

    assert job.status == JobStatus.SUCCEEDED
    output = job.output_json
    assert output["ingestion_run_id"]
    season_label = output["season_label"]

    season_id = _season_id_for_label(db_session, season_label)

    positions = db_session.execute(select(Position).where(Position.season_id == season_id)).scalars().all()
    assert len(positions) == 4

    teams = db_session.execute(select(Team).where(Team.season_id == season_id)).scalars().all()
    assert len(teams) == 20

    players = db_session.execute(select(Player).where(Player.season_id == season_id)).scalars().all()
    # 10 raw rows in the fixture, minus one dropped as a duplicate-id collision.
    assert len(players) == 9

    gameweeks = db_session.execute(select(Gameweek).where(Gameweek.season_id == season_id)).scalars().all()
    assert len(gameweeks) == 38

    fixtures = db_session.execute(select(Fixture).where(Fixture.season_id == season_id)).scalars().all()
    assert len(fixtures) == 4

    players_by_code = {p.fpl_code: p for p in players}
    assert players_by_code[900105].is_quarantined is True  # invalid team FK
    assert players_by_code[900105].team_id is None
    assert players_by_code[900106].is_quarantined is True  # negative minutes
    assert players_by_code[900107].is_quarantined is True  # impossible price
    assert (
        players_by_code[900110].is_quarantined is False
    )  # blank web_name is a warning, not quarantine-worthy

    clean_codes = {900101, 900102, 900103, 900104}
    for code in clean_codes:
        assert players_by_code[code].is_quarantined is False

    # Milestone 2 projection-input fields round-trip correctly from the fixture.
    player_900101 = players_by_code[900101]
    assert player_900101.points_per_game == Decimal("4.4")
    assert player_900101.ep_next == Decimal("8.8")
    assert player_900101.starts == 37
    assert player_900101.chance_of_playing_this_round is None  # fixture has null for this player
    player_900102 = players_by_code[900102]
    assert player_900102.chance_of_playing_this_round == 100

    ingestion_run_id = output["ingestion_run_id"]
    issues = (
        db_session.execute(
            select(DataQualityIssue).where(DataQualityIssue.ingestion_run_id == uuid.UUID(ingestion_run_id))
        )
        .scalars()
        .all()
    )
    check_names = {i.check_name for i in issues}
    assert "invalid_foreign_key" in check_names
    assert "negative_minutes" in check_names
    assert "impossible_price" in check_names
    assert "duplicate_fpl_id" in check_names
    assert "missing_required_field" in check_names

    error_issues = [i for i in issues if i.severity == DataQualitySeverity.ERROR]
    warning_issues = [i for i in issues if i.severity == DataQualitySeverity.WARNING]
    assert len(error_issues) == 5  # invalid_fk, negative_minutes, impossible_price, duplicate x2
    assert len(warning_issues) == 1  # blank web_name

    run_row = db_session.get(IngestionRun, uuid.UUID(ingestion_run_id))
    assert run_row.status == IngestionRunStatus.SUCCEEDED_WITH_WARNINGS
    assert run_row.report_markdown is not None
    assert run_row.data_quality_score == output["data_quality_score"]

    # score = 100 - 5*ERROR_PENALTY(5) - 1*WARNING_PENALTY(1) = 74 >= 70 threshold
    assert output["data_quality_score"] == 74.0
    assert output["optimization_permitted"] is True


def test_data_ingestion_records_price_signal_observations_for_every_non_quarantined_player(
    db_session, patched_fpl_client
):
    """app.ingestion.normalization.record_price_signal_observations -- one observation row per
    current-season, non-quarantined player, snapshotting the momentum inputs
    app.players.price_signal needs a historical record of to ever be calibratable (see that
    table's own module docstring). Quarantined players are deliberately excluded -- their
    transfers_in_event/selected_by_percent may be unreliable, same convention every other
    quarantine-respecting reader in this codebase already follows.
    """
    job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert job.status == JobStatus.SUCCEEDED
    season_id = _season_id_for_label(db_session, job.output_json["season_label"])

    observations = (
        db_session.execute(
            select(PlayerPriceSignalObservation).where(PlayerPriceSignalObservation.season_id == season_id)
        )
        .scalars()
        .all()
    )
    # 9 non-duplicate players ingested; 4 quarantined (900105 bad team FK, 900106 negative
    # minutes, 900107 impossible price, 900108 duplicate_fpl_id -- BOTH rows sharing a
    # colliding id get quarantined, even though only the first is actually upserted/kept) ->
    # 5 non-quarantined players, 5 observations.
    assert len(observations) == 5

    observed_player_ids = {o.player_id for o in observations}
    quarantined_players = (
        db_session.execute(
            select(Player).where(Player.season_id == season_id, Player.is_quarantined.is_(True))
        )
        .scalars()
        .all()
    )
    for player in quarantined_players:
        assert player.id not in observed_player_ids

    player_900101 = db_session.execute(
        select(Player).where(Player.season_id == season_id, Player.fpl_code == 900101)
    ).scalar_one()
    observation = next(o for o in observations if o.player_id == player_900101.id)
    assert observation.transfers_in_event == player_900101.transfers_in_event
    assert observation.transfers_out_event == player_900101.transfers_out_event
    assert observation.selected_by_percent == player_900101.selected_by_percent
