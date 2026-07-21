"""Integration tests for Milestone 5 Layer 2's RivalProfile computation, wired into LEAGUE_SYNC.

Reuses the exact same seeding/fixture setup as ``test_league_sync_job.py``
(the ~60-player optimization pool + the synthetic 4-entry H2H league) --
``patched_fpl_client_league`` additionally mocks ``fetch_manager_history``
(see that fixture's own docstring): entry B and "self" get a real 5-gameweek
history, entry C's history fetch cleanly 404s, and entry D's history fetch
raises -- independent of each entry's *picks* outcome, exactly matching
Layer 2's "reuse whatever history exists regardless of this run's
picks-fetch outcome" discipline.

``RivalProfile`` is upsert-in-place (current-state, keyed on
``(season_id, fpl_manager_id)`` -- see that model's own docstring), so row
counts scoped to this test file's season converge and are safe to assert
absolutely across repeated invocations, same convention as
``League``/``LeagueEntry``/``H2HMatch`` in ``test_league_sync_job.py``.
"""

from sqlalchemy import func, select

from app.jobs.service import execute_job
from packages.database.models.league import League
from packages.database.models.rival_profile import RivalProfile
from packages.database.models.season import Season
from packages.domain.h2h import RivalStateConfidence
from packages.domain.jobs import JobStatus, JobType, TriggerType
from tests.integration.conftest import (
    LEAGUE_RIVAL_B_FPL_MANAGER_ID,
    LEAGUE_RIVAL_C_FPL_MANAGER_ID,
    LEAGUE_RIVAL_D_FPL_MANAGER_ID,
    LEAGUE_SELF_FPL_MANAGER_ID,
)

FPL_LEAGUE_ID = 88889  # distinct from test_league_sync_job.py's 88888, but same fixture data


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


def _current_season_id(db_session):
    return db_session.execute(select(Season.id).where(Season.is_current.is_(True))).scalar_one()


def _profile(db_session, season_id, fpl_manager_id) -> RivalProfile:
    return db_session.execute(
        select(RivalProfile).where(
            RivalProfile.season_id == season_id, RivalProfile.fpl_manager_id == fpl_manager_id
        )
    ).scalar_one()


def test_rival_profiles_created_for_every_non_excluded_rival_with_sane_values(
    db_session, patched_fpl_client_optimization, patched_fpl_client_league
):
    _seed_players(db_session)

    job = _run_league_sync(db_session)

    assert job.status == JobStatus.SUCCEEDED
    assert job.output_json["rival_profiles_computed_count"] == 3  # B, C, D (self excluded)

    season_id = _current_season_id(db_session)
    league_id = db_session.execute(
        select(League.id).where(League.fpl_league_id == FPL_LEAGUE_ID)
    ).scalar_one()

    profile_count = db_session.execute(
        select(func.count()).select_from(RivalProfile).where(RivalProfile.season_id == season_id)
    ).scalar_one()
    assert profile_count == 3

    profile_b = _profile(db_session, season_id, LEAGUE_RIVAL_B_FPL_MANAGER_ID)
    assert profile_b.league_id == league_id
    assert profile_b.last_known_snapshot_id is not None  # B's picks resolved cleanly this run
    # Realistic cold-start reality: only a single gameweek's picks snapshot
    # and a thin (5-row) /history/ sample exist -- overall confidence must
    # be the honest floor, never a falsely-confident HIGH/MEDIUM.
    assert profile_b.overall_confidence == RivalStateConfidence.LOW
    assert profile_b.unknowns_json  # non-empty
    assert profile_b.evidence_json["history_available"] is True
    assert profile_b.evidence_json["history_current_row_count"] == 5
    # history.json's one wildcard play is at event 2, which only matches
    # wildcard's FIRST window (2-19 in the bootstrap-static sample) -- its
    # second window (20-38) is still open (the fixture's last finished event
    # is 38, not past it), so wildcard must still be reported as remaining.
    assert profile_b.derived_likely_remaining_chips_json == ["wildcard", "freehit", "bboost", "3xc"]
    for probability_field in (
        profile_b.predicted_no_transfer_probability,
        profile_b.predicted_one_transfer_probability,
        profile_b.predicted_multi_transfer_probability,
        profile_b.predicted_hit_probability,
    ):
        assert 0 <= probability_field <= 1

    profile_c = _profile(db_session, season_id, LEAGUE_RIVAL_C_FPL_MANAGER_ID)
    # C's picks are unavailable (404) AND its /history/ fetch also 404s in
    # this fixture -- no snapshot, no history: maximally unknown.
    assert profile_c.last_known_snapshot_id is None
    assert profile_c.overall_confidence == RivalStateConfidence.LOW
    assert profile_c.evidence_json["history_available"] is False
    assert profile_c.derived_free_transfers_min is None
    assert profile_c.derived_free_transfers_max is None
    # Honesty over convenience: full inventory, not an empty list, when
    # nothing is known about what's been played.
    assert set(profile_c.derived_likely_remaining_chips_json) == {"wildcard", "freehit", "bboost", "3xc"}

    profile_d = _profile(db_session, season_id, LEAGUE_RIVAL_D_FPL_MANAGER_ID)
    # D's picks fetch AND history fetch both raise (simulated client-level
    # failures) -- Layer 2 must still produce a (maximally degraded) profile
    # row, never abort the whole run.
    assert profile_d.overall_confidence == RivalStateConfidence.LOW
    assert profile_d.evidence_json["history_available"] is False
    assert any("history" in note.lower() for note in profile_d.unknowns_json)


def test_rival_profile_rerun_upserts_in_place_reflecting_latest_computation(
    db_session, patched_fpl_client_optimization, patched_fpl_client_league
):
    _seed_players(db_session)

    first_job = _run_league_sync(db_session)
    assert first_job.status == JobStatus.SUCCEEDED

    season_id = _current_season_id(db_session)
    first_computed_at = _profile(db_session, season_id, LEAGUE_RIVAL_B_FPL_MANAGER_ID).computed_at

    second_job = _run_league_sync(db_session)
    assert second_job.status == JobStatus.SUCCEEDED

    profile_count = db_session.execute(
        select(func.count()).select_from(RivalProfile).where(RivalProfile.season_id == season_id)
    ).scalar_one()
    assert profile_count == 3  # upsert-in-place: still exactly 3, not 6

    second_computed_at = _profile(db_session, season_id, LEAGUE_RIVAL_B_FPL_MANAGER_ID).computed_at
    assert second_computed_at > first_computed_at


def test_without_exclude_self_computes_a_profile_for_self_too(
    db_session, patched_fpl_client_optimization, patched_fpl_client_league
):
    _seed_players(db_session)

    job = _run_league_sync(db_session, exclude_self=False)

    assert job.status == JobStatus.SUCCEEDED
    assert job.output_json["rival_profiles_computed_count"] == 4

    season_id = _current_season_id(db_session)
    profile_self = _profile(db_session, season_id, LEAGUE_SELF_FPL_MANAGER_ID)
    assert profile_self.overall_confidence == RivalStateConfidence.LOW
