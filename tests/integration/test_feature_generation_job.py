"""Integration tests for the FEATURE_GENERATION job end-to-end through execute_job.

Requires a running Postgres with migrations applied (see conftest.py). Seeds
real data via a HISTORICAL_DATA_INGESTION run against the committed
``tests/fixtures/historical/2023-24/`` CSV files (the same fixture Layer 1's
own tests use — ``patched_historical_fpl_client`` in conftest.py), then runs
FEATURE_GENERATION against that seeded season. No real network access
happens in this module.
"""

from decimal import Decimal

from sqlalchemy import select

from app.features.service import generate_features_for_season
from app.jobs.service import execute_job
from packages.database.models.gameweek import Gameweek
from packages.database.models.player import Player
from packages.database.models.player_features import PlayerFeatures
from packages.database.models.season import Season
from packages.domain.jobs import JobStatus, JobType, TriggerType


def _season(db_session, label: str) -> Season:
    return db_session.execute(select(Season).where(Season.label == label)).scalar_one()


def _seed_historical_season(db_session):
    job = execute_job(
        db_session,
        job_type=JobType.HISTORICAL_DATA_INGESTION,
        parameters={"season_label": "2023-24"},
        trigger_type=TriggerType.LOCAL,
    )
    assert job.status == JobStatus.SUCCEEDED
    return _season(db_session, "2023-24")


def _run_feature_generation_job(db_session, season_label: str = "2023-24"):
    return execute_job(
        db_session,
        job_type=JobType.FEATURE_GENERATION,
        parameters={"season_label": season_label},
        trigger_type=TriggerType.LOCAL,
    )


def test_feature_generation_row_counts_and_shape(db_session, patched_historical_fpl_client):
    season = _seed_historical_season(db_session)

    job = _run_feature_generation_job(db_session)
    assert job.status == JobStatus.SUCCEEDED
    output = job.output_json
    assert output["season_labels"] == ["2023-24"]
    season_result = output["season_results"]["2023-24"]
    assert season_result["inserted"] + season_result["updated"] == 68

    gameweeks = db_session.execute(select(Gameweek).where(Gameweek.season_id == season.id)).scalars().all()
    assert len(gameweeks) == 4

    players = db_session.execute(select(Player).where(Player.season_id == season.id)).scalars().all()
    non_quarantined_players = [p for p in players if not p.is_quarantined]
    assert len(non_quarantined_players) == 17

    feature_rows = (
        db_session.execute(
            select(PlayerFeatures).where(
                PlayerFeatures.season_id == season.id, PlayerFeatures.feature_version == 1
            )
        )
        .scalars()
        .all()
    )
    # One row per (non-quarantined player, gameweek) combination — quarantined
    # players are excluded, matching this codebase's "active data" convention.
    assert len(feature_rows) == len(non_quarantined_players) * len(gameweeks)

    quarantined_player = next(p for p in players if p.is_quarantined)
    assert all(row.player_id != quarantined_player.id for row in feature_rows)

    for row in feature_rows:
        assert row.feature_version == 1
        assert row.data_source == "feature_engineering_v1"
        assert row.cutoff_timestamp is not None
        assert row.computed_at is not None


def test_feature_generation_spot_check_foden_round4(db_session, patched_historical_fpl_client):
    """Hand-computed spot check against Phil Foden's real 2023-24 rounds 1-4 stat lines.

    Foden (element 353) rows in tests/fixtures/historical/2023-24/gws/merged_gw.csv:
        round 1: total_points=3, minutes=90, was_home=False, kickoff=2023-08-11T19:00Z
        round 2: total_points=9, minutes=90, was_home=True,  kickoff=2023-08-19T19:00Z
        round 3: total_points=4, minutes=4,  was_home=False, kickoff=2023-08-27T13:00Z
        round 4: total_points=5, minutes=89, was_home=True,  kickoff=2023-09-02T14:00Z (fixture id 39,
                  team_h=13 (Man City) team_h_difficulty=2 team_a_difficulty=5 -> Foden's team is home,
                  so next_fixture_difficulty=2, next_fixture_was_home=True)

    Target = round 4. Prior (strictly earlier gameweeks) = rounds 1-3 only.
    """
    season = _seed_historical_season(db_session)
    job = _run_feature_generation_job(db_session)
    assert job.status == JobStatus.SUCCEEDED

    foden = db_session.execute(
        select(Player).where(Player.season_id == season.id, Player.fpl_element_id == 353)
    ).scalar_one()
    round4 = db_session.execute(
        select(Gameweek).where(Gameweek.season_id == season.id, Gameweek.fpl_event_id == 4)
    ).scalar_one()
    assert round4.finished is True  # historical seasons are always fully finished

    row = db_session.execute(
        select(PlayerFeatures).where(
            PlayerFeatures.player_id == foden.id,
            PlayerFeatures.gameweek_id == round4.id,
            PlayerFeatures.feature_version == 1,
        )
    ).scalar_one()

    # rounds 1-3: points 3, 9, 4 -> avg = 16/3 = 5.333... -> 5.33
    expected_avg = Decimal("5.33")
    assert row.rolling_avg_points_3gw == expected_avg
    assert row.rolling_avg_points_5gw == expected_avg  # only 3 gameweeks of history exist
    assert row.season_avg_points_to_date == expected_avg
    assert row.career_avg_points_prior_seasons is None  # no earlier season ingested in this test

    # home: round 2 only (9 points) -> 9.00; away: rounds 1 and 3 (3, 4) -> 3.50
    assert row.home_avg_points == Decimal("9.00")
    assert row.away_avg_points == Decimal("3.50")

    # most recent prior kickoff: round 3, 2023-08-27T13:00Z; target (round 4)
    # kickoff: 2023-09-02T14:00Z -> 6 days, 1 hour apart -> 6 whole days.
    assert row.rest_days == 6

    assert row.next_fixture_difficulty == 2
    assert row.next_fixture_was_home is True

    # round 3 deadline 2023-08-25T18:30Z, round 4 deadline 2023-09-01T18:30Z -> 7 days apart, a
    # normal week (the real international-break gap doesn't land until round 5, 14 days later).
    assert row.gameweek_schedule_gap_days == 7

    # round 4 already played (historical, finished=True) -> label populated,
    # equal to Foden's own round-4 total_points (single fixture, no DGW here).
    assert row.target_total_points == 5

    assert row.is_missing_history is False
    assert row.price_at_time == Decimal("76.0")  # most recent prior row (round 3) value=76
    assert row.ownership_at_time == 1332873  # round 3's selected count
    assert row.position_short_name == "MID"


def test_feature_generation_gameweek_schedule_gap_days_none_for_season_opening_gameweek(
    db_session, patched_historical_fpl_client
):
    """Boundary case: this season's own gameweek 1 has no previous gameweek to compare against,
    so the column must be None -- distinct from a real 0 (which would wrongly claim "back-to-back
    gameweeks with zero gap"). The real, non-null 7-day gap case (round 3 -> round 4) is covered
    by test_feature_generation_spot_check_foden_round4 above; this committed fixture only carries
    rounds 1-4 (see test_feature_generation_row_counts_and_shape's own `len(gameweeks) == 4`), so
    round 5's real 14-day international-break gap isn't reachable from this fixture."""
    season = _seed_historical_season(db_session)
    job = _run_feature_generation_job(db_session)
    assert job.status == JobStatus.SUCCEEDED

    foden = db_session.execute(
        select(Player).where(Player.season_id == season.id, Player.fpl_element_id == 353)
    ).scalar_one()
    round1 = db_session.execute(
        select(Gameweek).where(Gameweek.season_id == season.id, Gameweek.fpl_event_id == 1)
    ).scalar_one()

    row1 = db_session.execute(
        select(PlayerFeatures).where(
            PlayerFeatures.player_id == foden.id,
            PlayerFeatures.gameweek_id == round1.id,
            PlayerFeatures.feature_version == 1,
        )
    ).scalar_one()
    assert row1.gameweek_schedule_gap_days is None


def test_feature_generation_target_total_points_none_when_gameweek_not_yet_played(
    db_session, patched_historical_fpl_client
):
    """Simulates an in-progress gameweek (finished=False) and confirms the label is withheld."""
    season = _seed_historical_season(db_session)

    foden = db_session.execute(
        select(Player).where(Player.season_id == season.id, Player.fpl_element_id == 353)
    ).scalar_one()
    round4 = db_session.execute(
        select(Gameweek).where(Gameweek.season_id == season.id, Gameweek.fpl_event_id == 4)
    ).scalar_one()

    round4.finished = False
    db_session.commit()

    try:
        counts = generate_features_for_season(db_session, season.id, feature_version=2)
        db_session.commit()
        assert counts.inserted + counts.updated == 68

        row = db_session.execute(
            select(PlayerFeatures).where(
                PlayerFeatures.player_id == foden.id,
                PlayerFeatures.gameweek_id == round4.id,
                PlayerFeatures.feature_version == 2,
            )
        ).scalar_one()
        assert row.target_total_points is None
        # Everything computed strictly from prior (rounds 1-3) data is unaffected.
        assert row.rolling_avg_points_3gw == Decimal("5.33")
    finally:
        # Restore shared state — this integration suite runs against a
        # persistent, non-ephemeral Postgres, so other tests in this module
        # (and re-runs of this one) rely on round 4 being "finished" like
        # every other historical gameweek.
        round4.finished = True
        db_session.commit()


def test_feature_generation_is_idempotent(db_session, patched_historical_fpl_client):
    """Re-running never duplicates rows — the second run only ever updates, never inserts.

    Doesn't assume this is the first time ever feature_version=1 rows exist
    for this season in this (persistent, non-ephemeral) test database — other
    tests in this module may have already created them — only that running
    the job twice back-to-back is a stable, non-duplicating no-op the second
    time.
    """
    _seed_historical_season(db_session)
    season = _season(db_session, "2023-24")

    def _v1_row_ids():
        return set(
            db_session.execute(
                select(PlayerFeatures.id).where(
                    PlayerFeatures.season_id == season.id, PlayerFeatures.feature_version == 1
                )
            ).scalars()
        )

    first = _run_feature_generation_job(db_session)
    assert first.status == JobStatus.SUCCEEDED
    first_output = first.output_json["season_results"]["2023-24"]
    assert first_output["inserted"] + first_output["updated"] == 68
    first_row_ids = _v1_row_ids()
    assert len(first_row_ids) == 68

    second = _run_feature_generation_job(db_session)
    assert second.status == JobStatus.SUCCEEDED
    second_output = second.output_json["season_results"]["2023-24"]
    assert second_output["inserted"] == 0
    assert second_output["updated"] == 68

    second_row_ids = _v1_row_ids()
    assert len(second_row_ids) == 68
    assert first_row_ids == second_row_ids  # same rows updated in place, never duplicated
