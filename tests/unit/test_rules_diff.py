"""Unit tests for app.rules.diff.diff_rules_versions against synthetic snapshot pairs."""

from datetime import UTC, datetime

from app.rules.diff import diff_rules_versions
from app.rules.models import RuleField, RulesConfigSnapshot
from packages.domain.rules import RuleConfidence

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _snapshot(fields: list[RuleField], checksum: str = "abc") -> RulesConfigSnapshot:
    return RulesConfigSnapshot(fetched_at=NOW, checksum_sha256=checksum, fields=fields)


def test_previous_none_everything_added():
    fields = [
        RuleField("rules.squad_squadsize", 15, RuleConfidence.CONFIRMED, "src"),
        RuleField("rules.squad_team_limit", 3, RuleConfidence.CONFIRMED, "src"),
    ]
    diff = diff_rules_versions(None, _snapshot(fields))

    assert len(diff.added) == 2
    assert diff.removed == []
    assert diff.changed == []
    assert diff.confidence_changed == []


def test_field_added():
    previous = _snapshot([RuleField("rules.squad_squadsize", 15, RuleConfidence.CONFIRMED, "src")])
    current = _snapshot(
        [
            RuleField("rules.squad_squadsize", 15, RuleConfidence.CONFIRMED, "src"),
            RuleField("rules.new_field", 42, RuleConfidence.CONFIRMED, "src"),
        ]
    )
    diff = diff_rules_versions(previous, current)

    assert [f.path for f in diff.added] == ["rules.new_field"]
    assert diff.removed == []
    assert diff.changed == []


def test_field_removed():
    previous = _snapshot(
        [
            RuleField("rules.squad_squadsize", 15, RuleConfidence.CONFIRMED, "src"),
            RuleField("rules.old_field", 1, RuleConfidence.CONFIRMED, "src"),
        ]
    )
    current = _snapshot([RuleField("rules.squad_squadsize", 15, RuleConfidence.CONFIRMED, "src")])
    diff = diff_rules_versions(previous, current)

    assert diff.added == []
    assert [f.path for f in diff.removed] == ["rules.old_field"]
    assert diff.changed == []


def test_field_value_changed():
    previous = _snapshot([RuleField("rules.squad_team_limit", 3, RuleConfidence.CONFIRMED, "src")])
    current = _snapshot([RuleField("rules.squad_team_limit", 4, RuleConfidence.CONFIRMED, "src")])
    diff = diff_rules_versions(previous, current)

    assert diff.added == []
    assert diff.removed == []
    assert len(diff.changed) == 1
    old_field, new_field = diff.changed[0]
    assert old_field.value == 3
    assert new_field.value == 4
    assert diff.confidence_changed == []


def test_confidence_changed_same_value():
    previous = _snapshot(
        [RuleField("hand_maintained.transfer_hit_cost", -4, RuleConfidence.PROVISIONAL, "hand_maintained")]
    )
    current = _snapshot(
        [RuleField("hand_maintained.transfer_hit_cost", -4, RuleConfidence.CONFIRMED, "bootstrap_static")]
    )
    diff = diff_rules_versions(previous, current)

    assert diff.changed == []
    assert len(diff.confidence_changed) == 1
    old_field, new_field = diff.confidence_changed[0]
    assert old_field.confidence == RuleConfidence.PROVISIONAL
    assert new_field.confidence == RuleConfidence.CONFIRMED


def test_unchanged_field_is_not_reported():
    previous = _snapshot([RuleField("rules.squad_squadsize", 15, RuleConfidence.CONFIRMED, "src")])
    current = _snapshot([RuleField("rules.squad_squadsize", 15, RuleConfidence.CONFIRMED, "src")])
    diff = diff_rules_versions(previous, current)

    assert diff.added == []
    assert diff.removed == []
    assert diff.changed == []
    assert diff.confidence_changed == []
