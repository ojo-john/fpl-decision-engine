"""Integration tests for /players/{id}/availability-risk-notes and their effect on projections.

See ``packages.database.models.player_availability_risk_note``'s own docstring for why this
exists (a judgment-based, pre-official fitness/availability risk FPL's own official injury/doubt
data doesn't cover). These tests exercise the real HTTP CRUD surface plus confirm the note
actually discounts ``GET /players/{id}/projection`` -- not just that a row gets written.
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


def test_create_availability_risk_note_returns_the_created_row(db_session, patched_fpl_client_optimization):
    _seed(db_session)
    client = authed_client()
    player_id = _eligible_player_id(client)

    response = client.post(
        f"/players/{player_id}/availability-risk-notes",
        json={
            "reason": "Played through injury during an international tournament; late return.",
            "severity": "0.65",
            "valid_from_fpl_event_id": 1,
            "valid_until_fpl_event_id": 3,
            "created_by": "test",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["player_id"] == player_id
    assert body["severity"] == "0.65"
    assert body["valid_from_fpl_event_id"] == 1
    assert body["valid_until_fpl_event_id"] == 3
    assert body["created_by"] == "test"
    assert uuid.UUID(body["id"])


def test_create_availability_risk_note_404_for_unknown_player(db_session, patched_fpl_client_optimization):
    _seed(db_session)
    client = authed_client()

    response = client.post(
        f"/players/{uuid.uuid4()}/availability-risk-notes",
        json={"reason": "x", "severity": "0.5"},
    )

    assert response.status_code == 404


def test_create_availability_risk_note_422_when_window_is_backwards(
    db_session, patched_fpl_client_optimization
):
    _seed(db_session)
    client = authed_client()
    player_id = _eligible_player_id(client)

    response = client.post(
        f"/players/{player_id}/availability-risk-notes",
        json={"reason": "x", "severity": "0.5", "valid_from_fpl_event_id": 5, "valid_until_fpl_event_id": 2},
    )

    assert response.status_code == 422


# --- GET (list) ---------------------------------------------------------------------------------


def test_list_availability_risk_notes_empty_for_a_player_with_none(
    db_session, patched_fpl_client_optimization
):
    _seed(db_session)
    client = authed_client()
    player_id = _eligible_player_id(client)

    response = client.get(f"/players/{player_id}/availability-risk-notes")

    assert response.status_code == 200
    assert response.json() == []


def test_list_availability_risk_notes_returns_created_rows_most_recent_first(
    db_session, patched_fpl_client_optimization
):
    _seed(db_session)
    client = authed_client()
    player_id = _eligible_player_id(client)
    first = client.post(
        f"/players/{player_id}/availability-risk-notes", json={"reason": "first", "severity": "0.8"}
    ).json()
    second = client.post(
        f"/players/{player_id}/availability-risk-notes", json={"reason": "second", "severity": "0.6"}
    ).json()

    response = client.get(f"/players/{player_id}/availability-risk-notes")

    assert response.status_code == 200
    body = response.json()
    assert [row["id"] for row in body] == [second["id"], first["id"]]


def test_list_availability_risk_notes_404_for_unknown_player(db_session, patched_fpl_client_optimization):
    _seed(db_session)
    client = authed_client()

    response = client.get(f"/players/{uuid.uuid4()}/availability-risk-notes")

    assert response.status_code == 404


# --- DELETE -------------------------------------------------------------------------------------


def test_delete_availability_risk_note_removes_it(db_session, patched_fpl_client_optimization):
    _seed(db_session)
    client = authed_client()
    player_id = _eligible_player_id(client)
    created = client.post(
        f"/players/{player_id}/availability-risk-notes", json={"reason": "x", "severity": "0.5"}
    ).json()

    delete_response = client.delete(f"/players/{player_id}/availability-risk-notes/{created['id']}")
    list_response = client.get(f"/players/{player_id}/availability-risk-notes")

    assert delete_response.status_code == 204
    assert list_response.json() == []


def test_delete_availability_risk_note_404_for_mismatched_player(db_session, patched_fpl_client_optimization):
    _seed(db_session)
    client = authed_client()
    listed = client.get("/players", params={"page_size": 200}).json()["items"]
    player_a, player_b = listed[0]["id"], listed[1]["id"]
    created = client.post(
        f"/players/{player_a}/availability-risk-notes", json={"reason": "x", "severity": "0.5"}
    ).json()

    response = client.delete(f"/players/{player_b}/availability-risk-notes/{created['id']}")

    assert response.status_code == 404


def test_delete_availability_risk_note_404_for_unknown_note_id(db_session, patched_fpl_client_optimization):
    _seed(db_session)
    client = authed_client()
    player_id = _eligible_player_id(client)

    response = client.delete(f"/players/{player_id}/availability-risk-notes/{uuid.uuid4()}")

    assert response.status_code == 404


# --- Effect on GET /players/{id}/projection ------------------------------------------------------


def test_active_availability_risk_note_discounts_the_live_projection(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    _seed(db_session)
    client = authed_client()
    player_id = _eligible_player_id(client)
    current_event_id = _current_gameweek_event_id(db_session)

    baseline = client.get(f"/players/{player_id}/projection").json()
    assert "availability_risk_noted" not in baseline["source"]
    assert baseline["availability_risk_reason"] is None

    client.post(
        f"/players/{player_id}/availability-risk-notes",
        json={
            "reason": "Confirmed late return to training after an international tournament.",
            "severity": "0.5",
            "valid_from_fpl_event_id": current_event_id,
            "valid_until_fpl_event_id": current_event_id,
        },
    )

    noted = client.get(f"/players/{player_id}/projection").json()

    assert noted["source"] == f"{baseline['source']}_availability_risk_noted"
    assert noted["availability_risk_reason"] == (
        "Confirmed late return to training after an international tournament."
    )
    assert noted["availability_risk_severity"] == "0.50"
    assert noted["projected_points"] == baseline["projected_points"] * 0.5
    assert "Flagged availability risk" in noted["explanation"]


def test_availability_risk_note_outside_its_gameweek_window_does_not_apply(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    _seed(db_session)
    client = authed_client()
    player_id = _eligible_player_id(client)
    current_event_id = _current_gameweek_event_id(db_session)

    client.post(
        f"/players/{player_id}/availability-risk-notes",
        json={
            "reason": "Already resolved by the time this gameweek comes around.",
            "severity": "0.5",
            "valid_from_fpl_event_id": current_event_id + 10,
            "valid_until_fpl_event_id": current_event_id + 12,
        },
    )

    response = client.get(f"/players/{player_id}/projection").json()

    assert "availability_risk_noted" not in response["source"]
    assert response["availability_risk_reason"] is None


def test_two_overlapping_notes_the_more_severe_one_wins(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    _seed(db_session)
    client = authed_client()
    player_id = _eligible_player_id(client)
    current_event_id = _current_gameweek_event_id(db_session)

    client.post(
        f"/players/{player_id}/availability-risk-notes",
        json={
            "reason": "Mild concern.",
            "severity": "0.9",
            "valid_from_fpl_event_id": current_event_id,
            "valid_until_fpl_event_id": current_event_id,
        },
    )
    client.post(
        f"/players/{player_id}/availability-risk-notes",
        json={
            "reason": "Serious concern.",
            "severity": "0.4",
            "valid_from_fpl_event_id": current_event_id,
            "valid_until_fpl_event_id": current_event_id,
        },
    )

    response = client.get(f"/players/{player_id}/projection").json()

    assert response["availability_risk_reason"] == "Serious concern."
    assert response["availability_risk_severity"] == "0.40"
