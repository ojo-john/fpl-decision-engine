"""Unit tests for scripts/scheduler.py's pure decision functions.

No DB, no HTTP -- these take already-resolved plain state as arguments (see that module's own
docstring for why: a top-level scheduling boundary needs its "should this run now" logic to be
independently testable, not buried inside an untestable infinite loop).
"""

from datetime import UTC, datetime, timedelta

from scripts.scheduler import (
    GameweekState,
    event_ids_missing_authoritative_recommendation,
    gameweeks_needing_review,
    is_interval_due,
)

# --- gameweeks_needing_review -------------------------------------------------------------------


def test_gameweeks_needing_review_finished_and_unreviewed_is_due():
    gameweeks = [GameweekState(fpl_event_id=1, finished=True)]
    assert gameweeks_needing_review(gameweeks, already_reviewed_event_ids=set()) == [1]


def test_gameweeks_needing_review_finished_and_already_reviewed_is_not_due():
    gameweeks = [GameweekState(fpl_event_id=1, finished=True)]
    assert gameweeks_needing_review(gameweeks, already_reviewed_event_ids={1}) == []


def test_gameweeks_needing_review_not_yet_finished_is_not_due():
    gameweeks = [GameweekState(fpl_event_id=1, finished=False)]
    assert gameweeks_needing_review(gameweeks, already_reviewed_event_ids=set()) == []


def test_gameweeks_needing_review_multiple_gameweeks_only_returns_the_due_ones():
    gameweeks = [
        GameweekState(fpl_event_id=1, finished=True),  # already reviewed
        GameweekState(fpl_event_id=2, finished=True),  # due
        GameweekState(fpl_event_id=3, finished=False),  # not finished yet
    ]
    assert gameweeks_needing_review(gameweeks, already_reviewed_event_ids={1}) == [2]


def test_gameweeks_needing_review_results_sorted_ascending():
    gameweeks = [
        GameweekState(fpl_event_id=5, finished=True),
        GameweekState(fpl_event_id=2, finished=True),
        GameweekState(fpl_event_id=3, finished=True),
    ]
    assert gameweeks_needing_review(gameweeks, already_reviewed_event_ids=set()) == [2, 3, 5]


def test_gameweeks_needing_review_empty_input_returns_empty():
    assert gameweeks_needing_review([], already_reviewed_event_ids=set()) == []


# --- is_interval_due -----------------------------------------------------------------------------


def test_is_interval_due_never_run_before_is_always_due():
    now = datetime(2026, 8, 18, tzinfo=UTC)
    assert is_interval_due(None, now, timedelta(hours=4)) is True


def test_is_interval_due_interval_elapsed_is_due():
    now = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    last_run = now - timedelta(hours=5)
    assert is_interval_due(last_run, now, timedelta(hours=4)) is True


def test_is_interval_due_interval_not_yet_elapsed_is_not_due():
    now = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    last_run = now - timedelta(hours=1)
    assert is_interval_due(last_run, now, timedelta(hours=4)) is False


def test_is_interval_due_exactly_at_boundary_is_due():
    now = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    last_run = now - timedelta(hours=4)
    assert is_interval_due(last_run, now, timedelta(hours=4)) is True


# --- event_ids_missing_authoritative_recommendation ----------------------------------------------
# Real gap this closes: GAMEWEEK_REVIEW hard-refuses (NO_AUTHORITATIVE_RECOMMENDATION) without an
# existing is_authoritative=True recommendation_audits row for the target gameweek, and nothing
# previously created one automatically -- these prove the scheduler correctly identifies which due
# gameweeks need RECOMMENDATION_GENERATION run first, and which don't.


def test_due_event_without_authoritative_recommendation_needs_one():
    assert event_ids_missing_authoritative_recommendation([1], has_authoritative_ids=set()) == [1]


def test_due_event_with_existing_authoritative_recommendation_does_not_need_one():
    assert event_ids_missing_authoritative_recommendation([1], has_authoritative_ids={1}) == []


def test_multiple_due_events_only_the_missing_ones_are_returned():
    assert event_ids_missing_authoritative_recommendation(
        [1, 2, 3], has_authoritative_ids={2}
    ) == [1, 3]


def test_event_ids_missing_authoritative_recommendation_results_sorted_ascending():
    assert event_ids_missing_authoritative_recommendation(
        [5, 2, 3], has_authoritative_ids=set()
    ) == [2, 3, 5]


def test_event_ids_missing_authoritative_recommendation_empty_input_returns_empty():
    assert event_ids_missing_authoritative_recommendation([], has_authoritative_ids=set()) == []
