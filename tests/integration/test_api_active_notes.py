"""Integration tests for GET /players/notes/active.

Unlike every other note endpoint in this router, this one is NOT player-scoped -- it returns
every currently-active availability-risk/role-boost note across every player, for the current/
next unfinished gameweek. Built for external, read-only consumers that can't reach this app's
database directly (see .github/workflows/scout-watch.yml's dedup step) to know what's already
known before proposing something as new.
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


def _current_gameweek_event_id(db_session) -> int:
    season_id = db_session.execute(select(Season.id).where(Season.is_current.is_(True))).scalar_one()
    return db_session.execute(
        select(Gameweek.fpl_event_id).where(Gameweek.season_id == season_id, Gameweek.is_current.is_(True))
    ).scalar_one()


def _eligible_player_id(client) -> str:
    listed = client.get("/players", params={"page_size": 200}).json()["items"]
    return next(p for p in listed if p["status"] not in {"i", "s", "u"})["id"]


# --- Boundary cases -----------------------------------------------------------------------------


def test_no_current_season_returns_empty_list_not_404(db_session):
    """No current season -- explicitly cleared rather than assumed, since this repo's own
    shared, non-truncated Postgres may have a real is_current season left over from an earlier
    test/run in the same suite execution (see this module's sibling test files for the same
    established convention)."""
    db_session.execute(update(Season).values(is_current=False))
    db_session.commit()
    client = authed_client()

    response = client.get("/players/notes/active")

    assert response.status_code == 200
    assert response.json() == []


def test_no_active_gameweek_returns_empty_list(db_session, patched_fpl_client_optimization):
    """patched_fpl_client_optimization's own bootstrap sample has every gameweek finished (a
    realistic close-season snapshot) -- nothing "active" to evaluate a note's window against."""
    _seed(db_session)
    client = authed_client()

    response = client.get("/players/notes/active")

    assert response.status_code == 200
    assert response.json() == []


def test_no_notes_at_all_returns_empty_list(db_session, patched_fpl_client_optimization_with_active_gameweek):
    _seed(db_session)
    client = authed_client()

    response = client.get("/players/notes/active")

    assert response.status_code == 200
    assert response.json() == []


def test_note_whose_window_has_already_passed_is_excluded(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    _seed(db_session)
    client = authed_client()
    player_id = _eligible_player_id(client)
    current_event_id = _current_gameweek_event_id(db_session)
    assert current_event_id > 1  # guard: the fixture must leave room for an already-past window

    client.post(
        f"/players/{player_id}/availability-risk-notes",
        json={
            "reason": "Old injury, long since recovered.",
            "severity": "0.5",
            "valid_from_fpl_event_id": 1,
            "valid_until_fpl_event_id": current_event_id - 1,
        },
    )

    response = client.get("/players/notes/active")

    assert response.status_code == 200
    assert response.json() == []


def test_note_not_yet_started_is_excluded(db_session, patched_fpl_client_optimization_with_active_gameweek):
    _seed(db_session)
    client = authed_client()
    player_id = _eligible_player_id(client)
    current_event_id = _current_gameweek_event_id(db_session)

    client.post(
        f"/players/{player_id}/role-notes",
        json={
            "role": "penalties",
            "reason": "Rumoured future change, not yet in effect.",
            "severity": "1.2",
            "valid_from_fpl_event_id": current_event_id + 5,
        },
    )

    response = client.get("/players/notes/active")

    assert response.status_code == 200
    assert response.json() == []


# --- Happy path -----------------------------------------------------------------------------


def test_active_availability_risk_note_is_returned(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    _seed(db_session)
    client = authed_client()
    player_id = _eligible_player_id(client)
    current_event_id = _current_gameweek_event_id(db_session)

    client.post(
        f"/players/{player_id}/availability-risk-notes",
        json={
            "reason": "Carrying a minor knock.",
            "severity": "0.7",
            "valid_from_fpl_event_id": current_event_id,
            "valid_until_fpl_event_id": current_event_id,
            "created_by": "test",
        },
    )

    response = client.get("/players/notes/active")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    row = body[0]
    assert row["player_id"] == player_id
    assert row["category"] == "availability_risk"
    assert row["role"] is None
    assert row["reason"] == "Carrying a minor knock."
    assert row["severity"] == "0.70"
    assert row["created_by"] == "test"
    assert row["web_name"] is not None


def test_active_role_note_is_returned(db_session, patched_fpl_client_optimization_with_active_gameweek):
    _seed(db_session)
    client = authed_client()
    player_id = _eligible_player_id(client)
    current_event_id = _current_gameweek_event_id(db_session)

    client.post(
        f"/players/{player_id}/role-notes",
        json={
            "role": "penalties",
            "reason": "Confirmed as new first-choice penalty taker.",
            "severity": "1.15",
            "valid_from_fpl_event_id": current_event_id,
        },
    )

    response = client.get("/players/notes/active")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    row = body[0]
    assert row["player_id"] == player_id
    assert row["category"] == "role_boost"
    assert row["role"] == "penalties"
    assert row["reason"] == "Confirmed as new first-choice penalty taker."
    assert row["severity"] == "1.15"


def test_both_categories_across_different_players_are_returned_together(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    _seed(db_session)
    client = authed_client()
    listed = client.get("/players", params={"page_size": 200}).json()["items"]
    eligible = [p for p in listed if p["status"] not in {"i", "s", "u"}]
    player_a, player_b = eligible[0]["id"], eligible[1]["id"]
    current_event_id = _current_gameweek_event_id(db_session)

    client.post(
        f"/players/{player_a}/availability-risk-notes",
        json={
            "reason": "Risk note.",
            "severity": "0.6",
            "valid_from_fpl_event_id": current_event_id,
        },
    )
    client.post(
        f"/players/{player_b}/role-notes",
        json={
            "role": "corners",
            "reason": "Role note.",
            "severity": "1.1",
            "valid_from_fpl_event_id": current_event_id,
        },
    )

    response = client.get("/players/notes/active")

    assert response.status_code == 200
    body = response.json()
    categories_by_player = {row["player_id"]: row["category"] for row in body}
    assert categories_by_player[player_a] == "availability_risk"
    assert categories_by_player[player_b] == "role_boost"


def test_open_ended_note_with_no_valid_until_stays_active(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    """valid_until_fpl_event_id=None means "no known end yet" -- must still count as active for
    the current gameweek (role notes commonly use this, per that model's own docstring)."""
    _seed(db_session)
    client = authed_client()
    player_id = _eligible_player_id(client)
    current_event_id = _current_gameweek_event_id(db_session)

    client.post(
        f"/players/{player_id}/role-notes",
        json={
            "role": "free_kicks",
            "reason": "Long-term set-piece role, no known end date.",
            "severity": "1.1",
            "valid_from_fpl_event_id": current_event_id,
        },
    )

    response = client.get("/players/notes/active")

    assert response.status_code == 200
    assert len(response.json()) == 1
