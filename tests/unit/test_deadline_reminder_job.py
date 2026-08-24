"""Unit test for app.jobs.handlers.deadline_reminder's pure window-boundary check.

No DB -- resolve_target_gameweek/dedup/send logic all need a real database and are covered by
tests/integration/test_deadline_reminder_job.py instead (mirrors this repo's own
tests/unit/test_scheduling_tick.py + tests/integration/test_api_internal.py split).
"""

from datetime import UTC, datetime, timedelta

from app.jobs.handlers.deadline_reminder import REMINDER_WINDOW, is_within_reminder_window


def test_deadline_well_outside_window_is_not_due():
    now = datetime(2026, 8, 24, tzinfo=UTC)
    deadline = now + timedelta(hours=12)
    assert is_within_reminder_window(deadline, now) is False


def test_deadline_inside_window_is_due():
    now = datetime(2026, 8, 24, tzinfo=UTC)
    deadline = now + timedelta(hours=2)
    assert is_within_reminder_window(deadline, now) is True


def test_deadline_exactly_at_window_boundary_is_due():
    now = datetime(2026, 8, 24, tzinfo=UTC)
    deadline = now + REMINDER_WINDOW
    assert is_within_reminder_window(deadline, now) is True
