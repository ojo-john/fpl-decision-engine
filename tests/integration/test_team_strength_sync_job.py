"""Integration tests for TEAM_STRENGTH_SYNC against a real database.

Seeds real Team rows via DATA_INGESTION + ``patched_fpl_client_optimization`` (the real 20-team
``tests/fixtures/fpl_api/bootstrap_static_sample.json`` roster, including the real divergent
names -- "Man Utd", "Nott'm Forest", "Spurs" -- that make ``app.ingestion.team_strength``'s name
mapping a real integration risk, not just a unit-testable pure function). ClubEloClient itself is
monkeypatched (no real network access), mirroring this suite's own "patch the client class
method, keep everything else real" convention for external-API-backed jobs.
"""

from app.jobs.service import execute_job
from packages.clubelo_client.client import ClubEloClient, ClubEloFetchResult
from packages.database.models.season import Season
from packages.database.models.team import Team
from packages.domain.jobs import JobStatus, JobType, TriggerType

# Real ClubElo names for all 20 teams in tests/fixtures/fpl_api/bootstrap_static_sample.json,
# after applying app.ingestion.team_strength's real name-mapping (Man Utd -> Man United,
# Nott'm Forest -> Forest, Spurs -> Tottenham; every other name matches FPL's verbatim).
_FULL_ELO_SNAPSHOT_CSV = """Rank,Club,Country,Level,Elo,From,To
1,Arsenal,ENG,1,2063.0,2026-05-31,2026-08-21
2,Aston Villa,ENG,1,1920.0,2026-05-31,2026-08-23
3,Burnley,ENG,1,1700.0,2026-05-31,2026-08-23
4,Bournemouth,ENG,1,1871.0,2026-05-31,2026-08-23
5,Brentford,ENG,1,1836.0,2026-05-31,2026-08-22
6,Brighton,ENG,1,1842.0,2026-05-31,2026-08-23
7,Chelsea,ENG,1,1831.0,2026-05-31,2026-08-24
8,Crystal Palace,ENG,1,1803.0,2026-05-31,2026-08-22
9,Everton,ENG,1,1803.0,2026-05-31,2026-08-22
10,Fulham,ENG,1,1813.0,2026-05-31,2026-08-24
11,Leeds,ENG,1,1797.0,2026-05-31,2026-08-22
12,Liverpool,ENG,1,1910.0,2026-05-31,2026-08-23
13,Man City,ENG,1,1970.0,2026-05-31,2026-08-23
14,Man United,ENG,1,1915.0,2026-05-31,2026-08-22
15,Newcastle,ENG,1,1837.0,2026-05-31,2026-08-23
16,Forest,ENG,1,1822.0,2026-05-31,2026-08-22
17,Sunderland,ENG,1,1735.0,2026-05-31,2026-08-22
18,Tottenham,ENG,1,1777.0,2026-05-31,2026-08-22
19,West Ham,ENG,1,1750.0,2026-05-31,2026-08-22
20,Wolves,ENG,1,1740.0,2026-05-31,2026-08-22
2,Bayern,GER,1,2000.0,2026-05-21,2026-08-28
"""


def _seed_teams(db_session):
    ingestion_job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert ingestion_job.status == JobStatus.SUCCEEDED
    return ingestion_job


def _current_teams(db_session) -> list[Team]:
    season = db_session.query(Season).filter(Season.is_current.is_(True)).one()
    return db_session.query(Team).filter(Team.season_id == season.id).all()


def test_team_strength_sync_matches_all_real_teams_including_divergent_names(
    db_session, patched_fpl_client_optimization, monkeypatch
):
    _seed_teams(db_session)
    monkeypatch.setattr(
        ClubEloClient,
        "fetch_ratings_snapshot",
        lambda self, as_of_date: ClubEloFetchResult(
            raw_csv=_FULL_ELO_SNAPSHOT_CSV, http_status=200, attempt_count=1, error_message=None
        ),
    )

    job = execute_job(db_session, job_type=JobType.TEAM_STRENGTH_SYNC, trigger_type=TriggerType.LOCAL)

    assert job.status == JobStatus.SUCCEEDED
    assert job.output_json["teams_matched"] == 20
    assert job.output_json["teams_unmatched"] == []

    db_session.expire_all()
    teams_by_name = {t.name: t for t in _current_teams(db_session)}
    man_utd = teams_by_name["Man Utd"]
    assert man_utd.elo_rating == 1915
    assert man_utd.elo_last_synced_at is not None
    forest = teams_by_name["Nott'm Forest"]
    assert forest.elo_rating == 1822
    spurs = teams_by_name["Spurs"]
    assert spurs.elo_rating == 1777
    arsenal = teams_by_name["Arsenal"]
    assert arsenal.elo_rating == 2063


def test_team_strength_sync_partial_match_leaves_unmatched_teams_untouched(
    db_session, patched_fpl_client_optimization, monkeypatch
):
    """A real-world scenario: ClubElo hasn't rated a promoted/newly-added club yet. The sync must
    still succeed for every OTHER team, and must not silently invent a rating for the missing one."""
    _seed_teams(db_session)
    csv_missing_wolves = _FULL_ELO_SNAPSHOT_CSV.replace(
        "20,Wolves,ENG,1,1740.0,2026-05-31,2026-08-22\n", ""
    )
    monkeypatch.setattr(
        ClubEloClient,
        "fetch_ratings_snapshot",
        lambda self, as_of_date: ClubEloFetchResult(
            raw_csv=csv_missing_wolves, http_status=200, attempt_count=1, error_message=None
        ),
    )

    job = execute_job(db_session, job_type=JobType.TEAM_STRENGTH_SYNC, trigger_type=TriggerType.LOCAL)

    assert job.status == JobStatus.SUCCEEDED
    assert job.output_json["teams_matched"] == 19
    assert job.output_json["teams_unmatched"] == ["Wolves"]

    db_session.expire_all()
    teams_by_name = {t.name: t for t in _current_teams(db_session)}
    assert teams_by_name["Wolves"].elo_rating is None
    assert teams_by_name["Arsenal"].elo_rating == 2063


def test_team_strength_sync_fetch_failure_fails_the_job_without_partial_writes(
    db_session, patched_fpl_client_optimization, monkeypatch
):
    _seed_teams(db_session)
    monkeypatch.setattr(
        ClubEloClient,
        "fetch_ratings_snapshot",
        lambda self, as_of_date: ClubEloFetchResult(
            raw_csv=None, http_status=None, attempt_count=3, error_message="timeout: ConnectTimeout"
        ),
    )

    job = execute_job(db_session, job_type=JobType.TEAM_STRENGTH_SYNC, trigger_type=TriggerType.LOCAL)

    assert job.status == JobStatus.FAILED
    assert job.error_code == "CLUBELO_FETCH_FAILED"

    db_session.expire_all()
    for team in _current_teams(db_session):
        assert team.elo_rating is None
