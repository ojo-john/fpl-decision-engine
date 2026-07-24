"""Integration tests for the /players endpoints against a real database.

Uses ``db_session`` to seed data via ``execute_job`` and a separate
``TestClient``-driven request session (through ``apps.api.dependencies.get_db``)
to exercise the actual HTTP layer — both point at the same real Postgres, so
committed rows from one are visible to the other.
"""

import uuid

from app.jobs.service import execute_job
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
