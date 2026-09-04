"""Unit tests for the pure helper(s) in app.jobs.handlers.league_sync (no DB, no network)."""

from datetime import UTC, datetime

from app.jobs.handlers.league_sync import _derive_winner, classify_picks_availability
from packages.domain.h2h import PicksAvailability
from packages.domain.ingestion import EndpointHealth
from packages.fpl_client.models import FetchResult


def _match_row(**overrides: object) -> dict:
    base = {
        "entry_1_entry": 2001,
        "entry_2_entry": 2002,
        "entry_1_win": 0,
        "entry_2_win": 0,
        "winner": None,
    }
    base.update(overrides)
    return base


def test_derive_winner_trusts_raw_winner_when_fpl_provides_it():
    row = _match_row(winner=2001, entry_1_win=1)
    assert _derive_winner(row) == 2001


def test_derive_winner_falls_back_to_entry_1_win_when_raw_winner_is_null():
    row = _match_row(winner=None, entry_1_win=1, entry_2_win=0)
    assert _derive_winner(row) == 2001


def test_derive_winner_falls_back_to_entry_2_win_when_raw_winner_is_null():
    row = _match_row(winner=None, entry_1_win=0, entry_2_win=1)
    assert _derive_winner(row) == 2002


def test_derive_winner_is_none_for_a_genuine_draw():
    row = _match_row(winner=None, entry_1_win=0, entry_2_win=0)
    assert _derive_winner(row) is None


def test_derive_winner_is_none_for_a_match_that_has_not_been_played_yet():
    row = _match_row(winner=None, entry_1_win=None, entry_2_win=None)
    assert _derive_winner(row) is None


def _failed_fetch_result(http_status: int | None, error_message: str | None) -> FetchResult:
    return FetchResult(
        endpoint="manager_picks",
        request_url="https://fantasy.premierleague.com/api/entry/1/event/1/picks/",
        request_params=None,
        retrieved_at=datetime.now(UTC),
        http_status=http_status,
        attempt_count=1,
        latency_ms=10,
        health_status=EndpointHealth.BLOCKED,
        checksum_sha256=None,
        schema_signature=None,
        raw_body=None,
        error_message=error_message,
    )


def test_classify_picks_availability_404_is_unavailable_private_or_not_released():
    result = _failed_fetch_result(http_status=404, error_message="http_status_404")
    assert classify_picks_availability(result) == PicksAvailability.UNAVAILABLE_PRIVATE_OR_NOT_RELEASED


def test_classify_picks_availability_timeout_is_fetch_failed():
    result = _failed_fetch_result(http_status=None, error_message="timeout: ReadTimeout")
    assert classify_picks_availability(result) == PicksAvailability.FETCH_FAILED


def test_classify_picks_availability_connection_error_is_fetch_failed():
    result = _failed_fetch_result(http_status=None, error_message="connection_error: ConnectError")
    assert classify_picks_availability(result) == PicksAvailability.FETCH_FAILED


def test_classify_picks_availability_5xx_is_fetch_failed():
    result = _failed_fetch_result(http_status=503, error_message="http_status_503")
    assert classify_picks_availability(result) == PicksAvailability.FETCH_FAILED


def test_classify_picks_availability_non_404_4xx_is_fetch_failed():
    result = _failed_fetch_result(http_status=403, error_message="http_status_403")
    assert classify_picks_availability(result) == PicksAvailability.FETCH_FAILED
