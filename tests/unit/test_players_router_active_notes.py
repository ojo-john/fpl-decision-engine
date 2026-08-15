"""Unit tests for apps.api.routers.players._is_note_active (no DB).

The window-check logic itself, in isolation -- GET /players/notes/active's own integration
tests (tests/integration/test_api_active_notes.py) cover the full HTTP+DB behavior.
"""

from apps.api.routers.players import _is_note_active


def test_is_note_active_within_both_bounds():
    assert _is_note_active(3, valid_from=1, valid_until=5) is True


def test_is_note_active_before_valid_from_is_false():
    assert _is_note_active(3, valid_from=5, valid_until=None) is False


def test_is_note_active_after_valid_until_is_false():
    assert _is_note_active(6, valid_from=None, valid_until=5) is False


def test_is_note_active_exactly_on_valid_from_boundary_is_true():
    assert _is_note_active(5, valid_from=5, valid_until=None) is True


def test_is_note_active_exactly_on_valid_until_boundary_is_true():
    assert _is_note_active(5, valid_from=None, valid_until=5) is True


def test_is_note_active_with_no_bounds_at_all_is_always_true():
    assert _is_note_active(1, valid_from=None, valid_until=None) is True
    assert _is_note_active(38, valid_from=None, valid_until=None) is True
