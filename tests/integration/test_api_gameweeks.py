"""Integration tests for GET /gameweeks/next-deadline against a real database.

This endpoint exists independent of every gated optimization endpoint -- a
manager needs the real FPL deadline regardless of whether this system's own
optimization gate happens to be open, so it's tested the same way: on its
own, not as an appendage of the optimization test suite.
"""

from sqlalchemy import select, update

from app.jobs.service import execute_job
from app.optimization.gate import check_optimization_gate
from packages.database.models.season import Season
from packages.domain.jobs import JobStatus, JobType, TriggerType
from tests.integration.conftest import authed_client


def _seed(db_session):
    ingestion_job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert ingestion_job.status == JobStatus.SUCCEEDED
    return ingestion_job


def test_get_next_deadline_no_current_season_returns_404(db_session):
    # Shared, persistent local Postgres (see conftest.py's module docstring) -- explicitly clear
    # any pre-existing is_current row rather than assume a pristine DB.
    db_session.execute(update(Season).values(is_current=False))
    db_session.commit()
    client = authed_client()

    response = client.get("/gameweeks/next-deadline")

    assert response.status_code == 404
    assert response.json()["error_code"] == "NO_CURRENT_SEASON"


def test_get_next_deadline_off_season_returns_404(db_session, patched_fpl_client_optimization):
    """This fixture's bootstrap sample has every gameweek already finished (a realistic
    close-season snapshot) -- see patched_fpl_client_optimization's own docstring."""
    _seed(db_session)
    client = authed_client()

    response = client.get("/gameweeks/next-deadline")

    assert response.status_code == 404
    assert response.json()["error_code"] == "NO_UPCOMING_GAMEWEEK"


def test_get_next_deadline_returns_the_real_deadline_and_gate_verdict(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    _seed(db_session)
    season_id = db_session.execute(select(Season.id).where(Season.is_current.is_(True))).scalar_one()
    expected_gate = check_optimization_gate(db_session, season_id)
    client = authed_client()

    response = client.get("/gameweeks/next-deadline")

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["fpl_event_id"], int)
    assert isinstance(body["name"], str) and body["name"] != ""
    assert isinstance(body["deadline_time"], str)
    # Must reflect the SAME authoritative check every gated endpoint uses -- not an
    # approximation derived from is_current alone.
    assert body["gate_allowed"] == expected_gate.allowed
    assert body["gate_reason_code"] == expected_gate.reason_code
