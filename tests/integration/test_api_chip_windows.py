"""Integration tests for GET /optimize/chips/windows against a real database.

Distinct from ``test_api_chip_optimization.py``: that module covers a real
``CHIP_SIMULATION`` run (needs a squad, passes through the optimization
gate); this endpoint is a read-only, pre-season-available overview sourced
from the current season's ``rules_config.raw_chips_json`` -- see that
endpoint's own docstring.
"""

from sqlalchemy import select, update

from app.jobs.service import execute_job
from packages.database.models.gameweek import Gameweek
from packages.database.models.season import Season
from packages.domain.jobs import JobStatus, JobType, TriggerType
from tests.integration.conftest import authed_client


def _seed(db_session):
    ingestion_job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert ingestion_job.status == JobStatus.SUCCEEDED
    return ingestion_job


def test_get_chip_windows_no_current_season_returns_404(db_session):
    # Shared, persistent local Postgres (see conftest.py's module docstring) -- explicitly clear
    # any pre-existing is_current row rather than assume a pristine DB.
    db_session.execute(update(Season).values(is_current=False))
    db_session.commit()
    client = authed_client()

    response = client.get("/optimize/chips/windows")

    assert response.status_code == 404
    assert response.json()["error_code"] == "NO_CURRENT_SEASON"


def test_get_chip_windows_no_rules_config_returns_404(db_session):
    """A current season can exist with no rules_config row yet only via direct seeding --
    DATA_INGESTION's own RULES_SYNC step always creates one, so this state is otherwise
    unreachable through normal ingestion. Constructed directly to exercise the defensive check.
    """
    db_session.execute(update(Season).values(is_current=False))
    db_session.commit()
    db_session.add(Season(label="9999-00", is_current=True, total_players=1_000_000))
    db_session.commit()
    client = authed_client()

    response = client.get("/optimize/chips/windows")

    assert response.status_code == 404
    assert response.json()["error_code"] == "NO_RULES_CONFIG"


def test_get_chip_windows_resolves_real_windows_from_the_current_rules_config(
    db_session, patched_fpl_client_optimization
):
    """The committed bootstrap-static fixture has real wildcard/freehit/bboost/3xc windows --
    see tests/fixtures/fpl_api/bootstrap_static_sample.json's own "chips" array."""
    _seed(db_session)
    client = authed_client()

    response = client.get("/optimize/chips/windows")

    assert response.status_code == 200
    windows = response.json()
    assert len(windows) > 0
    chip_types = {w["chip_type"] for w in windows}
    assert chip_types == {"wildcard", "freehit", "bboost", "3xc"}
    for window in windows:
        assert window["start_event"] <= window["stop_event"]
        assert window["status"] in {"past", "open", "upcoming"}
        assert window["window_number"] >= 1


def test_get_chip_windows_status_reflects_the_current_gameweek(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    """With a real is_current gameweek, a window spanning it must be 'open', one entirely
    before it 'past', and one entirely after it 'upcoming' -- not every window uniformly
    'upcoming' the way the no-current-gameweek (pre-season) case is."""
    _seed(db_session)
    season_id = db_session.execute(select(Season.id).where(Season.is_current.is_(True))).scalar_one()
    current_event_id = db_session.execute(
        select(Gameweek.fpl_event_id).where(Gameweek.season_id == season_id, Gameweek.is_current.is_(True))
    ).scalar_one()
    client = authed_client()

    response = client.get("/optimize/chips/windows")

    assert response.status_code == 200
    windows = response.json()
    for window in windows:
        if window["start_event"] <= current_event_id <= window["stop_event"]:
            assert window["status"] == "open"
        elif current_event_id > window["stop_event"]:
            assert window["status"] == "past"
        else:
            assert window["status"] == "upcoming"
