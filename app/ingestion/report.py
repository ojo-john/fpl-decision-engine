"""Renders the full Markdown ingestion report for a DATA_INGESTION run.

Pure function — takes plain data (no ORM objects, no I/O) so it's directly
unit-testable and reusable wherever the report needs to be shown (DB column,
$GITHUB_STEP_SUMMARY, local file, build artifact).
"""

from datetime import datetime
from typing import Any

from app.data_quality.models import DataQualityIssue
from app.ingestion.normalization import NormalizationCounts


def _endpoint_health_table(endpoint_rows: list[dict[str, Any]]) -> str:
    if not endpoint_rows:
        return "_No endpoints fetched._\n"
    lines = [
        "| Endpoint | Health | HTTP status | Attempts | Latency (ms) | Schema drift |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in endpoint_rows:
        lines.append(
            f"| {row['endpoint']} | {row['health_status']} | {row.get('http_status')} | "
            f"{row.get('attempt_count')} | {row.get('latency_ms')} | {row.get('schema_drift_detected')} |"
        )
    return "\n".join(lines) + "\n"


def _normalization_table(normalization_summary: dict[str, NormalizationCounts]) -> str:
    lines = ["| Entity | Inserted | Updated | Quarantined |", "| --- | --- | --- | --- |"]
    for entity, counts in normalization_summary.items():
        lines.append(f"| {entity} | {counts.inserted} | {counts.updated} | {counts.quarantined} |")
    return "\n".join(lines) + "\n"


def _issues_by_severity_table(issues: list[DataQualityIssue]) -> str:
    if not issues:
        return "_No data quality issues found._\n"
    by_severity: dict[str, list[DataQualityIssue]] = {}
    for issue in issues:
        by_severity.setdefault(issue.severity.value, []).append(issue)

    lines = []
    for severity in ("critical", "error", "warning", "info"):
        group = by_severity.get(severity)
        if not group:
            continue
        lines.append(f"**{severity.upper()}** ({len(group)})")
        lines.append("")
        lines.append("| Entity | FPL id | Check | Message |")
        lines.append("| --- | --- | --- | --- |")
        for issue in group:
            lines.append(
                f"| {issue.entity_type} | {issue.entity_fpl_id} | {issue.check_name} | {issue.message} |"
            )
        lines.append("")
    return "\n".join(lines)


def render_historical_ingestion_report_markdown(
    ingestion_run_id: str,
    started_at: datetime,
    completed_at: datetime | None,
    status: str,
    season_labels: list[str],
    endpoint_rows: list[dict[str, Any]],
    normalization_summary: dict[str, NormalizationCounts],
    issues: list[DataQualityIssue],
    data_quality_score: float,
    optimization_permitted: bool,
) -> str:
    """Render the Markdown report for one HISTORICAL_DATA_INGESTION run.

    Reuses the same table-building helpers as the live ingestion report
    (``_endpoint_health_table``/``_normalization_table``/
    ``_issues_by_severity_table``) but omits the "rules changes" section
    entirely — there is no rules-versioning concept for historical seasons.
    """
    sections = [
        "# Historical Ingestion Report",
        "",
        "## Run",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| ingestion_run_id | {ingestion_run_id} |",
        "| run_type | historical_data_ingestion |",
        f"| seasons | {', '.join(season_labels)} |",
        f"| started_at | {started_at.isoformat()} |",
        f"| completed_at | {completed_at.isoformat() if completed_at else None} |",
        f"| status | {status} |",
        "",
        "## Endpoint health",
        "",
        _endpoint_health_table(endpoint_rows),
        "## Normalization summary",
        "",
        _normalization_table(normalization_summary),
        "## Data quality",
        "",
        f"Score: **{data_quality_score:.2f}** / 100",
        "",
        f"Optimization permitted: **{optimization_permitted}**",
        "",
        _issues_by_severity_table(issues),
    ]
    return "\n".join(sections)


def render_ingestion_report_markdown(
    ingestion_run_id: str,
    run_type: str,
    started_at: datetime,
    completed_at: datetime | None,
    status: str,
    endpoint_rows: list[dict[str, Any]],
    rules_changed: bool,
    rules_version_number: int,
    rules_report_markdown: str | None,
    normalization_summary: dict[str, NormalizationCounts],
    issues: list[DataQualityIssue],
    data_quality_score: float,
    optimization_permitted: bool,
) -> str:
    """Render the full Markdown ingestion report for one DATA_INGESTION run."""
    if rules_changed and rules_report_markdown:
        rules_section = rules_report_markdown
    else:
        rules_section = f"No rules changes detected since version {rules_version_number}.\n"

    sections = [
        "# Ingestion Report",
        "",
        "## Run",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| ingestion_run_id | {ingestion_run_id} |",
        f"| run_type | {run_type} |",
        f"| started_at | {started_at.isoformat()} |",
        f"| completed_at | {completed_at.isoformat() if completed_at else None} |",
        f"| status | {status} |",
        "",
        "## Endpoint health",
        "",
        _endpoint_health_table(endpoint_rows),
        "## Rules changes",
        "",
        rules_section,
        "",
        "## Normalization summary",
        "",
        _normalization_table(normalization_summary),
        "## Data quality",
        "",
        f"Score: **{data_quality_score:.2f}** / 100",
        "",
        f"Optimization permitted: **{optimization_permitted}**",
        "",
        _issues_by_severity_table(issues),
    ]
    return "\n".join(sections)
