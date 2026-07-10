"""Renders a RulesDiff as a Markdown change report."""

from app.rules.diff import RulesDiff
from app.rules.models import RuleField


def _added_removed_table(fields: list[RuleField]) -> str:
    if not fields:
        return "_None._\n"
    lines = ["| Path | Value | Confidence | Source |", "| --- | --- | --- | --- |"]
    for f in fields:
        lines.append(f"| `{f.path}` | `{f.value}` | {f.confidence.value} | {f.source} |")
    return "\n".join(lines) + "\n"


def _changed_table(pairs: list[tuple[RuleField, RuleField]]) -> str:
    if not pairs:
        return "_None._\n"
    lines = ["| Path | Old value | New value |", "| --- | --- | --- |"]
    for old, new in pairs:
        lines.append(f"| `{old.path}` | `{old.value}` | `{new.value}` |")
    return "\n".join(lines) + "\n"


def _confidence_changed_table(pairs: list[tuple[RuleField, RuleField]]) -> str:
    if not pairs:
        return "_None._\n"
    lines = ["| Path | Value | Old confidence | New confidence |", "| --- | --- | --- | --- |"]
    for old, new in pairs:
        lines.append(f"| `{old.path}` | `{new.value}` | {old.confidence.value} | {new.confidence.value} |")
    return "\n".join(lines) + "\n"


def render_rules_change_report_markdown(diff: RulesDiff, version_number: int) -> str:
    """Render a RulesDiff as a Markdown change report for rules_config version ``version_number``."""
    sections = [
        f"## Rules Config Change Report — version {version_number}",
        "",
        "### Added",
        "",
        _added_removed_table(diff.added),
        "### Removed",
        "",
        _added_removed_table(diff.removed),
        "### Changed",
        "",
        _changed_table(diff.changed),
        "### Confidence changes",
        "",
        _confidence_changed_table(diff.confidence_changed),
    ]
    return "\n".join(sections)
