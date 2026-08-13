"""Integration tests for POST /data/refresh against a real database.

Mirrors every other ``test_api_*.py`` module's own "duplicate a minimal
setup rather than import from the job-level test module" discipline -- this
tests the HTTP layer specifically (request/response shape), not
DATA_INGESTION's own ingestion logic, which is already covered in depth by
``test_data_ingestion_job.py``.
"""

from sqlalchemy import delete, select

from packages.database.models.player_price_signal_observation import PlayerPriceSignalObservation
from packages.database.models.season import Season
from packages.fpl_client import FPLClient
from tests.integration.conftest import authed_client


def test_refresh_data_happy_path(db_session, patched_fpl_client_optimization):
    client = authed_client()

    response = client.post("/data/refresh")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] in ("succeeded", "succeeded_with_warnings")
    assert body["season_label"]
    assert body["players_inserted"] + body["players_updated"] > 0

    # Delete this call's own player_price_signal_observations rows immediately -- its own
    # output is never otherwise read by this test, and left in place they leak into every
    # OTHER test's own season-scoped GAMEWEEK_REVIEW price-signal recalibration sample-size
    # count (see this repo's established cross-test-pollution precedent in
    # test_optimization_uses_champion.py's own player_features cleanup, same shared,
    # non-truncated Postgres).
    season_id = db_session.execute(select(Season.id).where(Season.label == body["season_label"])).scalar_one()
    db_session.execute(
        delete(PlayerPriceSignalObservation).where(PlayerPriceSignalObservation.season_id == season_id)
    )
    db_session.commit()


def test_refresh_data_fetch_failure_returns_409_not_500(db_session, monkeypatch):
    def _boom(self):
        raise RuntimeError("simulated FPL API outage")

    monkeypatch.setattr(FPLClient, "fetch_bootstrap_static", _boom)
    client = authed_client()

    response = client.post("/data/refresh")

    assert response.status_code == 409
    body = response.json()
    assert body["error_code"] == "DATA_INGESTION_FAILED"
    assert "job_id" in body
