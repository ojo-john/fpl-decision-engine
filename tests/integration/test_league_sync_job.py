"""Integration tests for the LEAGUE_SYNC job against a real database.

Seeds current-season players via DATA_INGESTION (reusing the ~60-player
optimization pool fixture, see ``build_optimization_bootstrap_json`` in
``conftest.py`` — every gameweek in that fixture is finished, a realistic
close-season snapshot), then runs LEAGUE_SYNC against a small, synthetic,
hand-verifiable 4-entry H2H league (``tests/fixtures/fpl_api/league/``, see
``patched_fpl_client_league`` in ``conftest.py``): entry A (2001) is "self"
and excluded (in most tests), entry B (2002)'s picks resolve cleanly, entry C
(2003)'s picks fetch returns a clean HTTP 404, and entry D (2004)'s picks
fetch raises (simulating an unexpected client-level failure).

Counting convention: ``League``/``LeagueEntry``/``H2HMatch`` are
upsert-in-place (see each model's own docstring), so asserting their exact
row counts scoped to this test file's fixed ``fpl_league_id``/``league_id``
is safe even across repeated suite invocations against the same persistent,
non-ephemeral local Postgres (see ``tests/integration/conftest.py``'s own
module docstring on that convention) — the count converges, it never grows.
``RivalGameweekSnapshot``/``LeagueSyncRun`` are insert-only audit tables with
no such convergence, so their assertions are deliberately delta-based
(count immediately before minus count immediately after, within one test),
rather than absolute, so a second invocation of this same test file against
a persistent database never produces a false failure.
"""

from datetime import UTC, datetime

from sqlalchemy import func, select

from app.jobs.service import execute_job
from packages.database.models.h2h_match import H2HMatch
from packages.database.models.league import League
from packages.database.models.league_entry import LeagueEntry
from packages.database.models.league_sync_run import LeagueSyncRun
from packages.database.models.rival_gameweek_snapshot import RivalGameweekSnapshot
from packages.domain.h2h import LeagueSyncRunStatus, LeagueType, PicksAvailability
from packages.domain.ingestion import EndpointHealth
from packages.domain.jobs import JobStatus, JobType, TriggerType
from packages.fpl_client import FetchResult, FPLClient
from tests.integration.conftest import (
    LEAGUE_RIVAL_B_FPL_MANAGER_ID,
    LEAGUE_RIVAL_C_FPL_MANAGER_ID,
    LEAGUE_RIVAL_D_FPL_MANAGER_ID,
    LEAGUE_SELF_FPL_MANAGER_ID,
)

FPL_LEAGUE_ID = 88888
ALL_SYNTHETIC_LEAGUE_MANAGER_IDS = [
    LEAGUE_SELF_FPL_MANAGER_ID,
    LEAGUE_RIVAL_B_FPL_MANAGER_ID,
    LEAGUE_RIVAL_C_FPL_MANAGER_ID,
    LEAGUE_RIVAL_D_FPL_MANAGER_ID,
]


def _seed_players(db_session):
    ingestion_job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert ingestion_job.status == JobStatus.SUCCEEDED
    return ingestion_job


def _run_league_sync(db_session, exclude_self: bool = True):
    parameters = {"fpl_league_id": FPL_LEAGUE_ID}
    if exclude_self:
        parameters["exclude_fpl_manager_id"] = LEAGUE_SELF_FPL_MANAGER_ID
    return execute_job(
        db_session,
        job_type=JobType.LEAGUE_SYNC,
        parameters=parameters,
        requested_by="integration-test",
        trigger_type=TriggerType.LOCAL,
    )


def _latest_snapshot(db_session, fpl_manager_id: int) -> RivalGameweekSnapshot:
    """The most recently created snapshot for a manager id.

    Not ``.scalar_one()`` -- across repeated suite invocations against a
    persistent database, more than one snapshot can legitimately exist for
    the same synthetic manager id (insert-only table, see this module's own
    docstring); only the latest one reflects what THIS test run just did.
    """
    return db_session.execute(
        select(RivalGameweekSnapshot)
        .where(RivalGameweekSnapshot.fpl_manager_id == fpl_manager_id)
        .order_by(RivalGameweekSnapshot.created_at.desc())
        .limit(1)
    ).scalar_one()


def _snapshot_count_for_synthetic_league(db_session) -> int:
    return db_session.execute(
        select(func.count())
        .select_from(RivalGameweekSnapshot)
        .where(RivalGameweekSnapshot.fpl_manager_id.in_(ALL_SYNTHETIC_LEAGUE_MANAGER_IDS))
    ).scalar_one()


def test_league_sync_happy_path_with_partial_rival_failure(
    db_session, patched_fpl_client_optimization, patched_fpl_client_league
):
    _seed_players(db_session)
    snapshot_count_before = _snapshot_count_for_synthetic_league(db_session)

    job = _run_league_sync(db_session)

    assert job.status == JobStatus.SUCCEEDED
    assert job.output_json["run_status"] == LeagueSyncRunStatus.SUCCEEDED_WITH_WARNINGS.value
    assert job.output_json["rivals_synced_count"] == 1
    assert job.output_json["rivals_unavailable_count"] == 2

    league = db_session.execute(select(League).where(League.fpl_league_id == FPL_LEAGUE_ID)).scalar_one()
    assert league.name == "Test H2H League"
    assert league.league_type == LeagueType.H2H
    assert league.admin_entry == LEAGUE_SELF_FPL_MANAGER_ID

    entries = (
        db_session.execute(select(LeagueEntry).where(LeagueEntry.league_id == league.id)).scalars().all()
    )
    assert len(entries) == 4
    entry_by_manager_id = {e.fpl_manager_id: e for e in entries}
    assert entry_by_manager_id[LEAGUE_SELF_FPL_MANAGER_ID].player_name == "Self Manager"
    assert entry_by_manager_id[LEAGUE_RIVAL_B_FPL_MANAGER_ID].rank == 2
    assert entry_by_manager_id[LEAGUE_RIVAL_C_FPL_MANAGER_ID].matches_drawn == 1
    assert entry_by_manager_id[LEAGUE_RIVAL_D_FPL_MANAGER_ID].points_for == 210

    matches = db_session.execute(select(H2HMatch).where(H2HMatch.league_id == league.id)).scalars().all()
    assert len(matches) == 2
    match_by_fpl_id = {m.fpl_match_id: m for m in matches}
    assert match_by_fpl_id[501].entry_1_id == LEAGUE_SELF_FPL_MANAGER_ID
    assert match_by_fpl_id[501].entry_2_id == LEAGUE_RIVAL_B_FPL_MANAGER_ID
    # Fixture's own raw "winner" is null (matches real FPL behavior for a regular-season match --
    # see H2HMatch's own docstring) -- this proves _derive_winner falls back to entry_1_win=1.
    assert match_by_fpl_id[501].winner == LEAGUE_SELF_FPL_MANAGER_ID
    assert match_by_fpl_id[502].entry_1_id == LEAGUE_RIVAL_C_FPL_MANAGER_ID
    assert match_by_fpl_id[502].entry_2_id == LEAGUE_RIVAL_D_FPL_MANAGER_ID
    # A genuine draw (entry_1_draw=entry_2_draw=1, both win flags 0) -- correctly stays undecided.
    assert match_by_fpl_id[502].winner is None

    # Exactly 3 new snapshots: self (2001) was excluded, B/C/D were synced.
    snapshot_count_after = _snapshot_count_for_synthetic_league(db_session)
    assert snapshot_count_after - snapshot_count_before == 3

    snapshot_b = _latest_snapshot(db_session, LEAGUE_RIVAL_B_FPL_MANAGER_ID)
    assert snapshot_b.picks_availability == PicksAvailability.AVAILABLE
    assert snapshot_b.http_status == 200
    assert snapshot_b.captain_element_id == 30031
    assert snapshot_b.vice_captain_element_id == 30049
    assert snapshot_b.squad_player_ids is not None
    assert len(snapshot_b.squad_player_ids) == 15
    assert snapshot_b.unresolved_fpl_element_ids_json == []
    assert snapshot_b.event_points == 55

    snapshot_c = _latest_snapshot(db_session, LEAGUE_RIVAL_C_FPL_MANAGER_ID)
    assert snapshot_c.picks_availability == PicksAvailability.UNAVAILABLE_PRIVATE_OR_NOT_RELEASED
    assert snapshot_c.http_status == 404
    assert snapshot_c.squad_player_ids is None
    assert snapshot_c.raw_picks_json is None

    snapshot_d = _latest_snapshot(db_session, LEAGUE_RIVAL_D_FPL_MANAGER_ID)
    assert snapshot_d.picks_availability == PicksAvailability.FETCH_FAILED
    assert snapshot_d.http_status is None
    assert snapshot_d.squad_player_ids is None
    assert snapshot_d.raw_picks_json is None

    run_row = db_session.execute(
        select(LeagueSyncRun)
        .where(LeagueSyncRun.league_id == league.id)
        .order_by(LeagueSyncRun.created_at.desc())
        .limit(1)
    ).scalar_one()
    assert run_row.run_status == LeagueSyncRunStatus.SUCCEEDED_WITH_WARNINGS
    assert run_row.rivals_synced_count == 1
    assert run_row.rivals_unavailable_count == 2
    assert run_row.completed_at is not None


def test_league_sync_without_exclude_self_syncs_every_entry(
    db_session, patched_fpl_client_optimization, patched_fpl_client_league
):
    """Documented behavior: omitting exclude_fpl_manager_id syncs every entry, including "self".

    Entry A (2001) isn't one of the mocked manager ids in
    ``patched_fpl_client_league``, so its picks fetch raises (the fixture's
    fallback branch) -- this exercises per-rival failure isolation (caught
    as FETCH_FAILED) rather than aborting the whole job, which stays
    SUCCEEDED (with warnings) overall.
    """
    _seed_players(db_session)
    snapshot_count_before = _snapshot_count_for_synthetic_league(db_session)

    job = _run_league_sync(db_session, exclude_self=False)

    assert job.status == JobStatus.SUCCEEDED
    assert job.output_json["run_status"] == LeagueSyncRunStatus.SUCCEEDED_WITH_WARNINGS.value
    assert job.output_json["rivals_synced_count"] == 1
    assert job.output_json["rivals_unavailable_count"] == 3

    snapshot_count_after = _snapshot_count_for_synthetic_league(db_session)
    assert snapshot_count_after - snapshot_count_before == 4

    snapshot_self = _latest_snapshot(db_session, LEAGUE_SELF_FPL_MANAGER_ID)
    assert snapshot_self.picks_availability == PicksAvailability.FETCH_FAILED


def test_league_sync_league_level_failure_creates_no_run_row(
    db_session, patched_fpl_client_optimization, monkeypatch
):
    _seed_players(db_session)

    def fetch_h2h_league_standings_404(self, league_id, page=1):
        return FetchResult(
            endpoint="h2h_league_standings",
            request_url=f"https://fantasy.premierleague.com/api/leagues-h2h/{league_id}/standings/",
            request_params={"page_standings": page},
            retrieved_at=datetime.now(UTC),
            http_status=404,
            attempt_count=1,
            latency_ms=10,
            health_status=EndpointHealth.BLOCKED,
            checksum_sha256=None,
            schema_signature=None,
            raw_body=None,
            error_message="http_status_404",
        )

    monkeypatch.setattr(FPLClient, "fetch_h2h_league_standings", fetch_h2h_league_standings_404)

    before_run_count = db_session.execute(select(func.count()).select_from(LeagueSyncRun)).scalar_one()
    before_league_count = db_session.execute(select(func.count()).select_from(League)).scalar_one()

    job = execute_job(
        db_session,
        job_type=JobType.LEAGUE_SYNC,
        parameters={"fpl_league_id": 999999999},
        trigger_type=TriggerType.LOCAL,
    )

    assert job.status == JobStatus.FAILED
    assert job.error_code == "LEAGUE_STANDINGS_FETCH_FAILED"

    after_run_count = db_session.execute(select(func.count()).select_from(LeagueSyncRun)).scalar_one()
    after_league_count = db_session.execute(select(func.count()).select_from(League)).scalar_one()
    # Deliberate choice: the standings fetch fails before any League or
    # LeagueSyncRun row is ever created -- see league_sync.py's own
    # module docstring.
    assert after_run_count == before_run_count
    assert after_league_count == before_league_count


def test_league_sync_rerun_upserts_current_state_but_inserts_new_audit_rows(
    db_session, patched_fpl_client_optimization, patched_fpl_client_league
):
    _seed_players(db_session)
    snapshot_count_before = _snapshot_count_for_synthetic_league(db_session)
    existing_league_id = db_session.execute(
        select(League.id).where(League.fpl_league_id == FPL_LEAGUE_ID)
    ).scalar_one_or_none()
    run_count_before = (
        0
        if existing_league_id is None
        else db_session.execute(
            select(func.count())
            .select_from(LeagueSyncRun)
            .where(LeagueSyncRun.league_id == existing_league_id)
        ).scalar_one()
    )

    first_job = _run_league_sync(db_session)
    assert first_job.status == JobStatus.SUCCEEDED

    second_job = _run_league_sync(db_session)
    assert second_job.status == JobStatus.SUCCEEDED

    league = db_session.execute(select(League).where(League.fpl_league_id == FPL_LEAGUE_ID)).scalar_one()

    # Upsert-in-place tables converge to the same row count regardless of
    # how many times this runs -- safe as absolute assertions.
    entry_count = db_session.execute(
        select(func.count()).select_from(LeagueEntry).where(LeagueEntry.league_id == league.id)
    ).scalar_one()
    assert entry_count == 4

    match_count = db_session.execute(
        select(func.count()).select_from(H2HMatch).where(H2HMatch.league_id == league.id)
    ).scalar_one()
    assert match_count == 2

    # Insert-only audit tables: one new set of rows per run -- exactly 2
    # runs' worth (3 rival snapshots each) were added by this test.
    snapshot_count_after = _snapshot_count_for_synthetic_league(db_session)
    assert snapshot_count_after - snapshot_count_before == 6

    run_count_after = db_session.execute(
        select(func.count()).select_from(LeagueSyncRun).where(LeagueSyncRun.league_id == league.id)
    ).scalar_one()
    assert run_count_after - run_count_before == 2
