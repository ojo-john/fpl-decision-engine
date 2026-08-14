"""Integration tests for /players/{id}/role-notes and their effect on projections.

See ``packages.database.models.player_role_note``'s own docstring for why this exists (a curated,
sourced set-piece/role-duty fact FPL's own bootstrap-static API doesn't expose) and why it is a
SEPARATE mechanism from ``player_availability_risk_notes``, not a reuse of that table. These tests
exercise the real HTTP CRUD surface, confirm a role note actually BOOSTS
``GET /players/{id}/projection`` (mirrors ``test_api_availability_risk_notes.py``'s own structure
closely), and -- the case that specifically justifies keeping these as two mechanisms -- confirm a
risk note and a role note BOTH apply when both are active for the same player/gameweek.
"""

import uuid

from sqlalchemy import select

from app.jobs.service import execute_job
from packages.database.models.gameweek import Gameweek
from packages.database.models.season import Season
from packages.domain.jobs import JobStatus, JobType, TriggerType
from tests.integration.conftest import authed_client


def _seed(db_session):
    ingestion_job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert ingestion_job.status == JobStatus.SUCCEEDED
    return ingestion_job


def _current_gameweek_event_id(db_session) -> int:
    season_id = db_session.execute(select(Season.id).where(Season.is_current.is_(True))).scalar_one()
    return db_session.execute(
        select(Gameweek.fpl_event_id).where(Gameweek.season_id == season_id, Gameweek.is_current.is_(True))
    ).scalar_one()


def _eligible_player_id(client) -> str:
    listed = client.get("/players", params={"page_size": 200}).json()["items"]
    return next(p for p in listed if p["status"] not in {"i", "s", "u"})["id"]


# --- POST -------------------------------------------------------------------------------------


def test_create_role_note_returns_the_created_row(db_session, patched_fpl_client_optimization):
    _seed(db_session)
    client = authed_client()
    player_id = _eligible_player_id(client)

    response = client.post(
        f"/players/{player_id}/role-notes",
        json={
            "role": "penalties",
            "reason": "Confirmed as new first-choice penalty taker after a change in duties.",
            "severity": "1.15",
            "valid_from_fpl_event_id": 1,
            "created_by": "test",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["player_id"] == player_id
    assert body["role"] == "penalties"
    assert body["severity"] == "1.15"
    assert body["valid_from_fpl_event_id"] == 1
    assert body["valid_until_fpl_event_id"] is None
    assert body["created_by"] == "test"
    assert uuid.UUID(body["id"])


def test_create_role_note_404_for_unknown_player(db_session, patched_fpl_client_optimization):
    _seed(db_session)
    client = authed_client()

    response = client.post(
        f"/players/{uuid.uuid4()}/role-notes",
        json={"role": "corners", "reason": "x", "severity": "1.1"},
    )

    assert response.status_code == 404


def test_create_role_note_422_when_window_is_backwards(db_session, patched_fpl_client_optimization):
    _seed(db_session)
    client = authed_client()
    player_id = _eligible_player_id(client)

    response = client.post(
        f"/players/{player_id}/role-notes",
        json={
            "role": "free_kicks",
            "reason": "x",
            "severity": "1.1",
            "valid_from_fpl_event_id": 5,
            "valid_until_fpl_event_id": 2,
        },
    )

    assert response.status_code == 422


# --- GET (list) ---------------------------------------------------------------------------------


def test_list_role_notes_empty_for_a_player_with_none(db_session, patched_fpl_client_optimization):
    _seed(db_session)
    client = authed_client()
    player_id = _eligible_player_id(client)

    response = client.get(f"/players/{player_id}/role-notes")

    assert response.status_code == 200
    assert response.json() == []


def test_list_role_notes_returns_created_rows_most_recent_first(db_session, patched_fpl_client_optimization):
    _seed(db_session)
    client = authed_client()
    player_id = _eligible_player_id(client)
    first = client.post(
        f"/players/{player_id}/role-notes",
        json={"role": "corners", "reason": "first", "severity": "1.1"},
    ).json()
    second = client.post(
        f"/players/{player_id}/role-notes",
        json={"role": "penalties", "reason": "second", "severity": "1.2"},
    ).json()

    response = client.get(f"/players/{player_id}/role-notes")

    assert response.status_code == 200
    body = response.json()
    assert [row["id"] for row in body] == [second["id"], first["id"]]


def test_list_role_notes_404_for_unknown_player(db_session, patched_fpl_client_optimization):
    _seed(db_session)
    client = authed_client()

    response = client.get(f"/players/{uuid.uuid4()}/role-notes")

    assert response.status_code == 404


# --- DELETE -------------------------------------------------------------------------------------


def test_delete_role_note_removes_it(db_session, patched_fpl_client_optimization):
    _seed(db_session)
    client = authed_client()
    player_id = _eligible_player_id(client)
    created = client.post(
        f"/players/{player_id}/role-notes",
        json={"role": "penalties", "reason": "x", "severity": "1.1"},
    ).json()

    delete_response = client.delete(f"/players/{player_id}/role-notes/{created['id']}")
    list_response = client.get(f"/players/{player_id}/role-notes")

    assert delete_response.status_code == 204
    assert list_response.json() == []


def test_delete_role_note_404_for_mismatched_player(db_session, patched_fpl_client_optimization):
    _seed(db_session)
    client = authed_client()
    listed = client.get("/players", params={"page_size": 200}).json()["items"]
    player_a, player_b = listed[0]["id"], listed[1]["id"]
    created = client.post(
        f"/players/{player_a}/role-notes",
        json={"role": "penalties", "reason": "x", "severity": "1.1"},
    ).json()

    response = client.delete(f"/players/{player_b}/role-notes/{created['id']}")

    assert response.status_code == 404


def test_delete_role_note_404_for_unknown_note_id(db_session, patched_fpl_client_optimization):
    _seed(db_session)
    client = authed_client()
    player_id = _eligible_player_id(client)

    response = client.delete(f"/players/{player_id}/role-notes/{uuid.uuid4()}")

    assert response.status_code == 404


# --- Effect on GET /players/{id}/projection ------------------------------------------------------


def test_active_role_note_boosts_the_live_projection(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    _seed(db_session)
    client = authed_client()
    player_id = _eligible_player_id(client)
    current_event_id = _current_gameweek_event_id(db_session)

    baseline = client.get(f"/players/{player_id}/projection").json()
    assert "role_boosted" not in baseline["source"]
    assert baseline["role_boost_reason"] is None

    client.post(
        f"/players/{player_id}/role-notes",
        json={
            "role": "penalties",
            "reason": "Confirmed as new first-choice penalty taker.",
            "severity": "1.2",
            "valid_from_fpl_event_id": current_event_id,
            "valid_until_fpl_event_id": current_event_id,
        },
    )

    boosted = client.get(f"/players/{player_id}/projection").json()

    assert boosted["source"] == f"{baseline['source']}_role_boosted"
    assert boosted["role_boost_reason"] == "Confirmed as new first-choice penalty taker."
    assert boosted["role_boost_severity"] == "1.20"
    assert boosted["projected_points"] == baseline["projected_points"] * 1.2
    assert "Boosted for a confirmed role" in boosted["explanation"]


def test_role_note_outside_its_gameweek_window_does_not_apply(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    _seed(db_session)
    client = authed_client()
    player_id = _eligible_player_id(client)
    current_event_id = _current_gameweek_event_id(db_session)

    client.post(
        f"/players/{player_id}/role-notes",
        json={
            "role": "penalties",
            "reason": "Not yet in effect.",
            "severity": "1.2",
            "valid_from_fpl_event_id": current_event_id + 10,
            "valid_until_fpl_event_id": current_event_id + 12,
        },
    )

    response = client.get(f"/players/{player_id}/projection").json()

    assert "role_boosted" not in response["source"]
    assert response["role_boost_reason"] is None


def test_two_overlapping_role_notes_the_strongest_boost_wins(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    """Inverse tie-break direction from availability-risk notes -- the HIGHEST severity wins for
    role notes, not the lowest. See get_active_role_boost_note's own docstring for why."""
    _seed(db_session)
    client = authed_client()
    player_id = _eligible_player_id(client)
    current_event_id = _current_gameweek_event_id(db_session)

    client.post(
        f"/players/{player_id}/role-notes",
        json={
            "role": "corners",
            "reason": "Modest boost.",
            "severity": "1.05",
            "valid_from_fpl_event_id": current_event_id,
            "valid_until_fpl_event_id": current_event_id,
        },
    )
    client.post(
        f"/players/{player_id}/role-notes",
        json={
            "role": "penalties",
            "reason": "Strong, confirmed boost.",
            "severity": "1.3",
            "valid_from_fpl_event_id": current_event_id,
            "valid_until_fpl_event_id": current_event_id,
        },
    )

    response = client.get(f"/players/{player_id}/projection").json()

    assert response["role_boost_reason"] == "Strong, confirmed boost."
    assert response["role_boost_severity"] == "1.30"


def test_availability_risk_note_and_role_note_both_apply_together(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    """The core reason these are two separate mechanisms: a player carrying a real fitness risk
    who has ALSO just been handed a set-piece role must get BOTH effects compounded in the live
    projection, not one silently overriding the other."""
    _seed(db_session)
    client = authed_client()
    player_id = _eligible_player_id(client)
    current_event_id = _current_gameweek_event_id(db_session)

    baseline = client.get(f"/players/{player_id}/projection").json()

    client.post(
        f"/players/{player_id}/availability-risk-notes",
        json={
            "reason": "Carrying a minor knock.",
            "severity": "0.8",
            "valid_from_fpl_event_id": current_event_id,
            "valid_until_fpl_event_id": current_event_id,
        },
    )
    client.post(
        f"/players/{player_id}/role-notes",
        json={
            "role": "penalties",
            "reason": "Just handed penalty duty.",
            "severity": "1.25",
            "valid_from_fpl_event_id": current_event_id,
            "valid_until_fpl_event_id": current_event_id,
        },
    )

    response = client.get(f"/players/{player_id}/projection").json()

    assert response["source"] == f"{baseline['source']}_availability_risk_noted_role_boosted"
    assert response["availability_risk_reason"] == "Carrying a minor knock."
    assert response["role_boost_reason"] == "Just handed penalty duty."
    assert response["projected_points"] == baseline["projected_points"] * 0.8 * 1.25
