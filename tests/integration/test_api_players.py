"""Integration tests for the /players endpoints against a real database.

Uses ``db_session`` to seed data via ``execute_job`` and a separate
``TestClient``-driven request session (through ``apps.api.dependencies.get_db``)
to exercise the actual HTTP layer — both point at the same real Postgres, so
committed rows from one are visible to the other.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import update

from app.jobs.service import execute_job
from packages.database.models.player import Player
from packages.database.models.price_signal_calibration import PriceSignalCalibration
from packages.database.models.season import Season
from packages.domain.jobs import JobStatus, JobType, TriggerType
from tests.integration.conftest import authed_client


def _seed(db_session):
    ingestion_job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert ingestion_job.status == JobStatus.SUCCEEDED
    return ingestion_job


def test_list_players_returns_paginated_current_season_players(db_session, patched_fpl_client_optimization):
    _seed(db_session)
    client = authed_client()

    response = client.get("/players", params={"page": 1, "page_size": 10})

    assert response.status_code == 200
    body = response.json()
    assert body["page"] == 1
    assert body["page_size"] == 10
    assert body["total"] >= 60
    assert len(body["items"]) == 10
    for item in body["items"]:
        assert item["is_quarantined"] is False
        assert isinstance(item["fpl_element_id"], int)
        # Same price_momentum enrichment GET /players/{id} already does, computed once per
        # request (not once per row) -- see list_players's own active-calibration lookup.
        assert item["price_momentum"] is not None
        assert item["price_momentum"]["momentum"] in {"likely_rise", "likely_fall", "stable", "unknown"}


def test_list_players_filters_by_position(db_session, patched_fpl_client_optimization):
    _seed(db_session)
    client = authed_client()

    response = client.get("/players", params={"position": "gkp", "page_size": 200})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] > 0
    assert all(item["position"] == "GKP" for item in body["items"])


def test_list_players_search_matches_web_name_substring(db_session, patched_fpl_client_optimization):
    _seed(db_session)
    client = authed_client()

    all_players = client.get("/players", params={"page_size": 200}).json()["items"]
    target = all_players[0]["web_name"]
    substring = target[: max(1, len(target) // 2)]

    response = client.get("/players", params={"search": substring, "page_size": 200})

    assert response.status_code == 200
    body = response.json()
    assert any(item["web_name"] == target for item in body["items"])
    assert all(substring.lower() in item["web_name"].lower() for item in body["items"])


def test_get_player_detail_returns_full_row(db_session, patched_fpl_client_optimization):
    _seed(db_session)
    client = authed_client()
    listed = client.get("/players", params={"page_size": 1}).json()["items"][0]

    response = client.get(f"/players/{listed['id']}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == listed["id"]
    assert "is_quarantined" in body
    assert "quarantine_reason" in body
    # Same team/position enrichment PlayerSummary already does for the list view -- a detail
    # page needs readable names too, not just team_id/position_id.
    assert body["team_short_name"] == listed["team_short_name"]
    assert body["position"] == listed["position"]


def test_get_player_detail_404_for_unknown_id(db_session, patched_fpl_client_optimization):
    _seed(db_session)
    client = authed_client()

    response = client.get(f"/players/{uuid.uuid4()}")

    assert response.status_code == 404


def test_get_player_projection_shape(db_session, patched_fpl_client_optimization):
    # Endpoint-plumbing test only — project_player()'s actual eligibility/
    # source logic is covered exhaustively in tests/unit/test_projection.py.
    # The synthetic 60-player pool has varied statuses, so pick a page large
    # enough to guarantee at least one eligible ('a'/'d' status) player
    # rather than assuming the alphabetically-first one happens to be.
    _seed(db_session)
    client = authed_client()
    listed_players = client.get("/players", params={"page_size": 200}).json()["items"]
    eligible = next(p for p in listed_players if p["status"] not in {"i", "s", "u"})

    response = client.get(f"/players/{eligible['id']}/projection")

    assert response.status_code == 200
    body = response.json()
    assert body["player_id"] == eligible["id"]
    assert isinstance(body["projected_points"], float)
    assert body["is_eligible"] is True
    assert body["projection_formula_version"] >= 1
    # "Why this projection" breakdown fields (added post-review) -- this fixture has every
    # gameweek finished (no active gameweek), so is_cold_start_season is always False here
    # regardless of player_gameweek_stats state -- see get_player_projection's own
    # target_gameweek_id is None short-circuit.
    assert body["is_cold_start_season"] is False
    assert body["champion_model_family"] is None
    assert body["champion_model_version_id"] is None
    assert isinstance(body["explanation"], str) and body["explanation"] != ""


def test_get_player_projection_cold_start_breakdown(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    # This fixture leaves the current season's last gameweek unfinished (a real target gameweek
    # resolves) but seeds no player_gameweek_stats rows at all -- a genuine cold-start season
    # (see app.optimization.projection._is_cold_start_season). Unlike
    # test_optimization_uses_champion.py's tests, no player_gameweek_stats row is seeded here
    # on purpose: this test asserts is_cold_start_season reflects the real season state.
    _seed(db_session)
    client = authed_client()
    listed_players = client.get("/players", params={"page_size": 200}).json()["items"]
    eligible = next(p for p in listed_players if p["status"] not in {"i", "s", "u"})

    response = client.get(f"/players/{eligible['id']}/projection")

    assert response.status_code == 200
    body = response.json()
    assert body["is_cold_start_season"] is True
    # No champion has ever been selected in this test's own seeding path, so the cold-start
    # blend never actually engages (project_player_with_champion only consults
    # _cold_start_projection once a champion exists) -- champion_model_family must stay None.
    assert body["champion_model_family"] is None
    assert body["champion_model_version_id"] is None
    assert isinstance(body["explanation"], str) and body["explanation"] != ""
    # next_fixture_difficulty is a schedule fact, resolvable even with zero current-season
    # history -- present for a player with an upcoming fixture in this fixture's data.
    assert body["next_fixture_difficulty"] is None or isinstance(body["next_fixture_difficulty"], int)


# --- GET /players/{id}/price-history ---------------------------------------------------------


def test_get_player_price_history_404_for_unknown_id(db_session, patched_fpl_client_optimization):
    _seed(db_session)
    client = authed_client()

    response = client.get(f"/players/{uuid.uuid4()}/price-history")

    assert response.status_code == 404


def test_get_player_price_history_empty_when_no_change_yet(db_session, patched_fpl_client_optimization):
    _seed(db_session)
    client = authed_client()
    listed = client.get("/players", params={"page_size": 1}).json()["items"][0]

    response = client.get(f"/players/{listed['id']}/price-history")

    assert response.status_code == 200
    assert response.json() == []


def test_get_player_price_history_returns_a_real_observed_change(
    db_session, patched_fpl_client_optimization
):
    _seed(db_session)
    client = authed_client()
    listed = client.get("/players", params={"page_size": 1}).json()["items"][0]
    original_cost = listed["now_cost"]

    elements = patched_fpl_client_optimization["bootstrap"]["elements"]
    element = next(e for e in elements if e["id"] == listed["fpl_element_id"])
    element["now_cost"] = original_cost + 3

    second = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert second.status == JobStatus.SUCCEEDED

    response = client.get(f"/players/{listed['id']}/price-history")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["old_cost"] == original_cost
    assert body[0]["new_cost"] == original_cost + 3


# --- GET /players/{id}: price_momentum respects an active PriceSignalCalibration ---------------


def test_get_player_detail_price_momentum_uses_active_calibration_thresholds(
    db_session, patched_fpl_client_optimization
):
    """A tuned, active price_signal_calibrations row must actually change the classification a
    real request sees -- not just be persisted and ignored. net=1000, ratio=0.002: STABLE under
    the hardcoded default ratio_threshold (0.02), but LIKELY_RISE once an active calibration
    tunes it down to 0.001 (same numbers test_classify_price_momentum_custom_ratio_threshold_
    flips_stable_to_likely already proves at the pure-function level -- this proves the router
    wiring that makes it real).
    """
    # Shared, persistent local Postgres (see conftest.py's module docstring) -- explicitly clear
    # any pre-existing is_active row rather than assume a pristine DB, same convention this
    # codebase already uses for is_champion/is_current/is_priority in other test modules.
    db_session.execute(update(PriceSignalCalibration).values(is_active=False))
    db_session.commit()

    _seed(db_session)
    client = authed_client()
    listed = client.get("/players", params={"page_size": 1}).json()["items"][0]

    player = db_session.get(Player, uuid.UUID(listed["id"]))
    player.transfers_in_event = 1500
    player.transfers_out_event = 500
    player.selected_by_percent = Decimal("50.0")
    season = db_session.get(Season, player.season_id)
    season.total_players = 1_000_000
    db_session.commit()

    baseline = client.get(f"/players/{listed['id']}")
    assert baseline.json()["price_momentum"]["momentum"] == "stable"

    db_session.add(
        PriceSignalCalibration(
            ratio_threshold=Decimal("0.001"),
            absolute_floor=1000,
            sample_size=40,
            accuracy=Decimal("1.0000"),
            is_active=True,
            calibrated_at=datetime.now(UTC),
        )
    )
    db_session.commit()

    response = client.get(f"/players/{listed['id']}")

    assert response.status_code == 200
    momentum = response.json()["price_momentum"]
    assert momentum["momentum"] == "likely_rise"
    assert momentum["net_transfers_event"] == 1000


def test_list_players_price_momentum_uses_active_calibration_thresholds(
    db_session, patched_fpl_client_optimization
):
    """Same calibration-respecting behavior as the detail endpoint's own test above, proven at
    the list endpoint too -- list_players hoists the active-calibration lookup to once per
    request (not once per row), so this also guards against that hoisting accidentally reusing
    a stale/wrong value across rows.
    """
    db_session.execute(update(PriceSignalCalibration).values(is_active=False))
    db_session.commit()

    _seed(db_session)
    client = authed_client()
    listed = client.get("/players", params={"page_size": 1}).json()["items"][0]

    player = db_session.get(Player, uuid.UUID(listed["id"]))
    player.transfers_in_event = 1500
    player.transfers_out_event = 500
    player.selected_by_percent = Decimal("50.0")
    season = db_session.get(Season, player.season_id)
    season.total_players = 1_000_000
    db_session.commit()

    baseline = client.get("/players", params={"search": listed["web_name"]})
    assert baseline.json()["items"][0]["price_momentum"]["momentum"] == "stable"

    db_session.add(
        PriceSignalCalibration(
            ratio_threshold=Decimal("0.001"),
            absolute_floor=1000,
            sample_size=40,
            accuracy=Decimal("1.0000"),
            is_active=True,
            calibrated_at=datetime.now(UTC),
        )
    )
    db_session.commit()

    response = client.get("/players", params={"search": listed["web_name"]})

    assert response.status_code == 200
    momentum = response.json()["items"][0]["price_momentum"]
    assert momentum["momentum"] == "likely_rise"
    assert momentum["net_transfers_event"] == 1000
