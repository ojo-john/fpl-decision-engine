"""Unit tests for app.data_quality.checks, each against small in-memory row lists."""

from datetime import UTC, datetime, timedelta

from app.data_quality import checks
from packages.domain.ingestion import DataQualitySeverity


def test_check_duplicate_fpl_ids_flags_all_matching_rows():
    rows = [{"id": 1}, {"id": 2}, {"id": 1}]
    issues = checks.check_duplicate_fpl_ids(rows, entity_type="player")

    assert len(issues) == 2
    assert all(i.check_name == "duplicate_fpl_id" for i in issues)
    assert all(i.severity == DataQualitySeverity.ERROR for i in issues)


def test_check_duplicate_fpl_ids_no_issue_when_unique():
    rows = [{"id": 1}, {"id": 2}]
    assert checks.check_duplicate_fpl_ids(rows, entity_type="player") == []


def test_check_invalid_foreign_keys_flags_missing_reference():
    rows = [{"id": 1, "team": 9999}, {"id": 2, "team": 1}]
    issues = checks.check_invalid_foreign_keys(
        rows, entity_type="player", fk_specs=[("team", {1, 2, 3}, "team")]
    )

    assert len(issues) == 1
    assert issues[0].entity_fpl_id == 1
    assert issues[0].check_name == "invalid_foreign_key"


def test_check_invalid_foreign_keys_allows_none():
    rows = [{"id": 1, "team": None}]
    issues = checks.check_invalid_foreign_keys(
        rows, entity_type="player", fk_specs=[("team", {1, 2}, "team")]
    )
    assert issues == []


def test_check_impossible_prices_flags_non_positive():
    rows = [{"id": 1, "now_cost": 0}, {"id": 2, "now_cost": -5}, {"id": 3, "now_cost": 45}]
    issues = checks.check_impossible_prices(rows)

    assert {i.entity_fpl_id for i in issues} == {1, 2}
    assert all(i.check_name == "impossible_price" for i in issues)


def test_check_negative_minutes_flags_negative():
    rows = [{"id": 1, "minutes": -10}, {"id": 2, "minutes": 0}]
    issues = checks.check_negative_minutes(rows)

    assert len(issues) == 1
    assert issues[0].entity_fpl_id == 1


def test_check_invalid_scores_flags_negative_score():
    rows = [{"id": 1, "team_h_score": -1, "team_a_score": 2, "finished": True}]
    issues = checks.check_invalid_scores(rows)
    assert len(issues) == 1
    assert issues[0].check_name == "invalid_score"


def test_check_invalid_scores_flags_finished_with_null_score():
    rows = [{"id": 1, "team_h_score": None, "team_a_score": None, "finished": True}]
    issues = checks.check_invalid_scores(rows)
    assert len(issues) == 1


def test_check_invalid_scores_allows_unfinished_null_score():
    rows = [{"id": 1, "team_h_score": None, "team_a_score": None, "finished": False}]
    assert checks.check_invalid_scores(rows) == []


def test_check_gameweek_consistency_allows_null_event():
    rows = [{"id": 1, "event": None}]
    assert checks.check_gameweek_consistency(rows, valid_gameweek_ids={1, 2}) == []


def test_check_gameweek_consistency_flags_unknown_gameweek():
    rows = [{"id": 1, "event": 99}]
    issues = checks.check_gameweek_consistency(rows, valid_gameweek_ids={1, 2})
    assert len(issues) == 1
    assert issues[0].check_name == "invalid_gameweek_reference"


def test_check_at_most_one_current_gameweek_ok():
    rows = [{"id": 1, "is_current": False}, {"id": 2, "is_current": True}]
    assert checks.check_at_most_one_current_gameweek(rows) == []


def test_check_at_most_one_current_gameweek_zero_is_critical():
    rows = [{"id": 1, "is_current": False}]
    issues = checks.check_at_most_one_current_gameweek(rows)
    assert len(issues) == 1
    assert issues[0].severity == DataQualitySeverity.CRITICAL


def test_check_at_most_one_current_gameweek_multiple_is_critical():
    rows = [{"id": 1, "is_current": True}, {"id": 2, "is_current": True}]
    issues = checks.check_at_most_one_current_gameweek(rows)
    assert len(issues) == 1
    assert issues[0].severity == DataQualitySeverity.CRITICAL


def test_check_team_count_exactly_20_ok():
    assert checks.check_team_count([{"id": i} for i in range(20)]) == []


def test_check_team_count_wrong_count_critical():
    issues = checks.check_team_count([{"id": i} for i in range(19)])
    assert len(issues) == 1
    assert issues[0].severity == DataQualitySeverity.CRITICAL


def test_check_squad_composition_math_ok():
    rows = [{"squad_select": 2}, {"squad_select": 5}, {"squad_select": 5}, {"squad_select": 3}]
    assert checks.check_squad_composition_math(rows, expected_squad_size=15) == []


def test_check_squad_composition_math_mismatch_critical():
    rows = [{"squad_select": 2}, {"squad_select": 5}]
    issues = checks.check_squad_composition_math(rows, expected_squad_size=15)
    assert len(issues) == 1
    assert issues[0].severity == DataQualitySeverity.CRITICAL


def test_check_stale_ingestion_no_prior_run_no_issue():
    assert checks.check_stale_ingestion(None, now=datetime.now(UTC)) == []


def test_check_stale_ingestion_recent_no_issue():
    now = datetime(2026, 1, 2, tzinfo=UTC)
    last = now - timedelta(hours=1)
    assert checks.check_stale_ingestion(last, now=now) == []


def test_check_stale_ingestion_old_warns():
    now = datetime(2026, 1, 2, tzinfo=UTC)
    last = now - timedelta(hours=30)
    issues = checks.check_stale_ingestion(last, now=now)
    assert len(issues) == 1
    assert issues[0].severity == DataQualitySeverity.WARNING


def test_check_schema_drift_flags_when_detected():
    issues = checks.check_schema_drift("bootstrap_static", schema_drift_detected=True)
    assert len(issues) == 1
    assert issues[0].severity == DataQualitySeverity.WARNING


def test_check_schema_drift_no_issue_when_not_detected():
    assert checks.check_schema_drift("bootstrap_static", schema_drift_detected=False) == []


def test_check_missing_required_fields_flags_blank_and_none():
    rows = [{"id": 1, "web_name": ""}, {"id": 2, "web_name": None}, {"id": 3, "web_name": "Salah"}]
    issues = checks.check_missing_required_fields(rows, entity_type="player", required_fields=("web_name",))
    assert {i.entity_fpl_id for i in issues} == {1, 2}
