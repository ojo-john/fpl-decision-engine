"""Diffing two RulesConfigSnapshot versions by field path."""

from dataclasses import dataclass

from app.rules.models import RuleField, RulesConfigSnapshot


@dataclass
class RulesDiff:
    added: list[RuleField]
    removed: list[RuleField]
    changed: list[tuple[RuleField, RuleField]]  # same path, different value
    confidence_changed: list[tuple[RuleField, RuleField]]  # same path+value, different confidence


def diff_rules_versions(previous: RulesConfigSnapshot | None, current: RulesConfigSnapshot) -> RulesDiff:
    """Diff two rules snapshots by field path.

    If ``previous`` is ``None`` (no prior version exists), every field in
    ``current`` is reported as ``added``.
    """
    if previous is None:
        return RulesDiff(added=list(current.fields), removed=[], changed=[], confidence_changed=[])

    previous_by_path = {f.path: f for f in previous.fields}
    current_by_path = {f.path: f for f in current.fields}

    previous_paths = set(previous_by_path.keys())
    current_paths = set(current_by_path.keys())

    added = [current_by_path[path] for path in sorted(current_paths - previous_paths)]
    removed = [previous_by_path[path] for path in sorted(previous_paths - current_paths)]

    changed: list[tuple[RuleField, RuleField]] = []
    confidence_changed: list[tuple[RuleField, RuleField]] = []
    for path in sorted(previous_paths & current_paths):
        old_field = previous_by_path[path]
        new_field = current_by_path[path]
        if old_field.value != new_field.value:
            changed.append((old_field, new_field))
        elif old_field.confidence != new_field.confidence:
            confidence_changed.append((old_field, new_field))

    return RulesDiff(added=added, removed=removed, changed=changed, confidence_changed=confidence_changed)
