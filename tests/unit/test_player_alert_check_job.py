"""Unit tests for app.jobs.handlers.player_alert_check's pure diff-decision functions.

No DB -- resolving current-season players/PlayerAlertState rows/sending a real push all need a
real database and are covered by tests/integration/test_player_alert_check_job.py instead (mirrors
this repo's own tests/unit + tests/integration split for app.scheduling.tick/deadline_reminder).
"""

from app.jobs.handlers.player_alert_check import is_material_momentum_change, is_material_status_change

# --- is_material_status_change -------------------------------------------------------------------


def test_unchanged_status_is_not_material():
    assert is_material_status_change("a", "a") is False


def test_newly_injured_is_material():
    assert is_material_status_change("a", "i") is True


def test_recovered_from_injury_is_material():
    assert is_material_status_change("i", "a") is True


def test_severity_change_between_two_at_risk_statuses_is_material():
    assert is_material_status_change("d", "i") is True


def test_unknown_to_unknown_is_not_material():
    assert is_material_status_change(None, None) is False


# --- is_material_momentum_change ------------------------------------------------------------------


def test_unchanged_momentum_is_not_material():
    assert is_material_momentum_change("stable", "stable") is False


def test_stable_to_likely_rise_is_material():
    assert is_material_momentum_change("stable", "likely_rise") is True


def test_likely_rise_to_stable_is_material():
    assert is_material_momentum_change("likely_rise", "stable") is True


def test_reversal_from_rise_to_fall_is_material():
    assert is_material_momentum_change("likely_rise", "likely_fall") is True


def test_unknown_to_stable_is_not_material():
    assert is_material_momentum_change("unknown", "stable") is False
