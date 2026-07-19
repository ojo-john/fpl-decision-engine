"""Pure composition/rendering for one GAMEWEEK_REVIEW run's ``learning_reports`` row.

Milestone 7 Layer 4. Mirrors ``app.ingestion.report``'s own "plain data in,
Markdown string out" convention exactly -- no ORM objects, no I/O, no
free-text generation, so this is directly unit-testable and reusable
wherever the review needs to be shown (DB column, API response, a future
notification channel).

Two entry points, both PURE:

- ``compose_notable_findings``: a structured, BOUNDED list built MECHANICALLY
  from already-computed ``FailureAttributionSummary``/``LearningSignalSummary``
  data -- one entry per non-``RECOMMENDATION_WAS_SOUND`` failure attribution,
  one per ``threshold_breached=True`` learning signal. Section 31's "notify
  only on material findings" principle: an all-sound, no-breach review
  produces an EMPTY list, never a manufactured "nothing to report" entry.
- ``render_gameweek_review_report_markdown``: the full human-readable
  Markdown summary -- gameweek reviewed, what was recommended and with what
  confidence, what actually happened, the counterfactual highlights, the
  failure attributions, and the learning signals with their breach status.

Milestone 7 Layer 6 (Recursive Learning Loop: BACKTEST job + the
GAMEWEEK_REVIEW -> MODEL_TRAINING retraining-trigger wiring) adds
``RetrainingTriggerSummary`` below, and folds it into both entry points
additively: ``compose_notable_findings`` gets an optional third
``retraining`` parameter (default ``None`` -- every pre-existing call site
keeps working unchanged) that appends one more mechanically-derived
``"retraining_triggered"`` finding whenever a retraining attempt actually
happened this run, and ``render_gameweek_review_report_markdown`` gets a new
"Retraining trigger" section. See ``app.jobs.handlers.gameweek_review``'s own
module docstring for exactly when this fires.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

# RECOMMENDATION_WAS_SOUND is a plain string literal here (not an imported enum member) to keep this
# module's own dataclasses framework/enum-agnostic, matching app.ingestion.report's own "plain data
# in" convention -- callers pass FailureReasonCode.value strings, never the enum itself.
_RECOMMENDATION_WAS_SOUND_REASON_CODE = "recommendation_was_sound"


@dataclass(frozen=True)
class FailureAttributionSummary:
    """Plain-data mirror of one ``FailureAttribution`` row -- see that model's own docstring."""

    reason_code: str
    category: str
    severity: float | None
    evidence: dict[str, Any] | None


@dataclass(frozen=True)
class LearningSignalSummary:
    """Plain-data mirror of one computed ``LearningSignalData``/``LearningSignal`` row."""

    signal_type: str
    window_size_gameweeks: int
    value: float
    sample_count: int
    threshold_breached: bool


@dataclass(frozen=True)
class CounterfactualResultSummary:
    """Plain-data mirror of one ``CounterfactualResult`` row."""

    comparison_type: str
    baseline_points: float
    alternative_points: float
    delta: float
    is_value_adding: bool


@dataclass(frozen=True)
class RecommendationSummary:
    """What was recommended for this gameweek, and how confidently -- see
    ``RecommendationAudit``'s own docstring for the source fields.
    """

    confidence: str
    component_summaries: list[str]
    known_risks: list[str]


@dataclass(frozen=True)
class OutcomeSummary:
    """What actually happened -- see ``RecommendationOutcome``'s own docstring for the source fields."""

    actual_xi_points: int | None
    actual_captain_points: int | None
    actual_bench_points: int | None
    h2h_result: str | None
    actual_chip_played: str | None
    recommendation_net_gain: float | None


@dataclass(frozen=True)
class RetrainingTriggerSummary:
    """What happened, if anything, when this run's own ``ROLLING_PROJECTION_MAE`` learning signal
    came back with ``threshold_breached=True`` (Milestone 7 Layer 6).

    ``triggered=False`` for the overwhelmingly common case (no breach this
    run) -- every other field is ``None`` in that case. When ``triggered`` is
    ``True``, ``model_training_status``/``error`` describe whether the
    triggered ``MODEL_TRAINING`` job itself succeeded (a training-side
    failure is recorded here, never propagated as this GAMEWEEK_REVIEW run's
    own failure -- see ``app.jobs.handlers.gameweek_review``'s own module
    docstring), and ``promotion_verdict`` is populated only when training
    succeeded AND a shadow challenger/champion pair existed to run
    ``app.modeling.promotion.evaluate_promotion`` against.
    """

    triggered: bool
    model_training_job_id: str | None = None
    model_training_status: str | None = None
    shadow_challenger_model_version_id: str | None = None
    promotion_verdict: str | None = None
    promotion_id: str | None = None
    error: str | None = None


def compose_notable_findings(
    failure_attributions: list[FailureAttributionSummary],
    learning_signals: list[LearningSignalSummary],
    retraining: RetrainingTriggerSummary | None = None,
) -> list[dict[str, Any]]:
    """A bounded, mechanically-derived list of what's worth surfacing from this run.

    Never free-text-generated -- every entry is a direct, structured read of
    already-computed data. See module docstring for the exact inclusion
    rules -- a ``retraining_triggered`` entry is appended only when
    ``retraining`` is given AND ``retraining.triggered`` is ``True``.
    """
    findings: list[dict[str, Any]] = []
    for attribution in failure_attributions:
        if attribution.reason_code == _RECOMMENDATION_WAS_SOUND_REASON_CODE:
            continue
        findings.append(
            {
                "type": "material_failure",
                "reason_code": attribution.reason_code,
                "category": attribution.category,
                "severity": attribution.severity,
                "evidence": attribution.evidence,
            }
        )
    for signal in learning_signals:
        if not signal.threshold_breached:
            continue
        findings.append(
            {
                "type": "signal_breached",
                "signal_type": signal.signal_type,
                "value": signal.value,
                "sample_count": signal.sample_count,
                "window_size_gameweeks": signal.window_size_gameweeks,
            }
        )
    if retraining is not None and retraining.triggered:
        findings.append(
            {
                "type": "retraining_triggered",
                "model_training_job_id": retraining.model_training_job_id,
                "model_training_status": retraining.model_training_status,
                "shadow_challenger_model_version_id": retraining.shadow_challenger_model_version_id,
                "promotion_verdict": retraining.promotion_verdict,
                "error": retraining.error,
            }
        )
    return findings


def _run_table(
    learning_report_id: str,
    target_fpl_event_id: int,
    started_at: datetime,
    completed_at: datetime | None,
    status: str,
) -> str:
    lines = [
        "| Field | Value |",
        "| --- | --- |",
        f"| learning_report_id | {learning_report_id} |",
        f"| gameweek | GW{target_fpl_event_id} |",
        f"| started_at | {started_at.isoformat()} |",
        f"| completed_at | {completed_at.isoformat() if completed_at else None} |",
        f"| status | {status} |",
    ]
    return "\n".join(lines) + "\n"


def _recommendation_section(recommendation: RecommendationSummary) -> str:
    lines = [f"Confidence: **{recommendation.confidence}**", ""]
    if recommendation.component_summaries:
        lines.append("What was recommended:")
        lines.append("")
        for summary in recommendation.component_summaries:
            lines.append(f"- {summary}")
        lines.append("")
    if recommendation.known_risks:
        lines.append("Known risks at generation time:")
        lines.append("")
        for risk in recommendation.known_risks:
            lines.append(f"- {risk}")
        lines.append("")
    return "\n".join(lines)


def _outcome_section(outcome: OutcomeSummary) -> str:
    lines = [
        "| Field | Value |",
        "| --- | --- |",
        f"| actual_xi_points | {outcome.actual_xi_points} |",
        f"| actual_captain_points | {outcome.actual_captain_points} |",
        f"| actual_bench_points | {outcome.actual_bench_points} |",
        f"| h2h_result | {outcome.h2h_result} |",
        f"| actual_chip_played | {outcome.actual_chip_played} |",
        f"| recommendation_net_gain | {outcome.recommendation_net_gain} |",
    ]
    return "\n".join(lines) + "\n"


def _counterfactuals_table(results: list[CounterfactualResultSummary]) -> str:
    if not results:
        return "_No counterfactual comparisons were computable for this gameweek._\n"
    lines = [
        "| Comparison | Baseline | Alternative | Delta | Value-adding? |",
        "| --- | --- | --- | --- | --- |",
    ]
    for result in results:
        lines.append(
            f"| {result.comparison_type} | {result.baseline_points} | {result.alternative_points} | "
            f"{result.delta} | {result.is_value_adding} |"
        )
    return "\n".join(lines) + "\n"


def _failure_attributions_table(attributions: list[FailureAttributionSummary]) -> str:
    if not attributions:
        return "_No failure attributions recorded._\n"
    lines = [
        "| Reason code | Category | Severity | Evidence |",
        "| --- | --- | --- | --- |",
    ]
    for attribution in attributions:
        lines.append(
            f"| {attribution.reason_code} | {attribution.category} | {attribution.severity} | "
            f"{attribution.evidence} |"
        )
    return "\n".join(lines) + "\n"


def _learning_signals_table(signals: list[LearningSignalSummary]) -> str:
    if not signals:
        return "_No learning signals computed._\n"
    lines = [
        "| Signal | Value | Sample count | Window (GWs) | Threshold breached |",
        "| --- | --- | --- | --- | --- |",
    ]
    for signal in signals:
        lines.append(
            f"| {signal.signal_type} | {signal.value} | {signal.sample_count} | "
            f"{signal.window_size_gameweeks} | {signal.threshold_breached} |"
        )
    return "\n".join(lines) + "\n"


def _notable_findings_section(findings: list[dict[str, Any]]) -> str:
    if not findings:
        return "_No material findings this gameweek -- the recommendation held up._\n"
    lines = []
    for finding in findings:
        if finding["type"] == "material_failure":
            lines.append(
                f"- **Material failure**: {finding['reason_code']} ({finding['category']}), "
                f"severity={finding['severity']}, evidence={finding['evidence']}"
            )
        elif finding["type"] == "signal_breached":
            lines.append(
                f"- **Signal breached**: {finding['signal_type']} = {finding['value']} "
                f"(sample_count={finding['sample_count']})"
            )
        else:  # "retraining_triggered"
            lines.append(
                f"- **Retraining triggered**: model_training_job_id={finding['model_training_job_id']}, "
                f"status={finding['model_training_status']}, "
                f"promotion_verdict={finding['promotion_verdict']}, error={finding['error']}"
            )
    return "\n".join(lines) + "\n"


def _retraining_trigger_section(retraining: RetrainingTriggerSummary | None) -> str:
    if retraining is None or not retraining.triggered:
        return "_No retraining triggered this gameweek._\n"
    lines = [
        "| Field | Value |",
        "| --- | --- |",
        f"| model_training_job_id | {retraining.model_training_job_id} |",
        f"| model_training_status | {retraining.model_training_status} |",
        f"| shadow_challenger_model_version_id | {retraining.shadow_challenger_model_version_id} |",
        f"| promotion_verdict | {retraining.promotion_verdict} |",
        f"| promotion_id | {retraining.promotion_id} |",
        f"| error | {retraining.error} |",
    ]
    return "\n".join(lines) + "\n"


def render_gameweek_review_report_markdown(
    learning_report_id: str,
    target_fpl_event_id: int,
    started_at: datetime,
    completed_at: datetime | None,
    status: str,
    recommendation: RecommendationSummary,
    outcome: OutcomeSummary,
    counterfactual_results: list[CounterfactualResultSummary],
    failure_attributions: list[FailureAttributionSummary],
    learning_signals: list[LearningSignalSummary],
    notable_findings: list[dict[str, Any]],
    retraining: RetrainingTriggerSummary | None = None,
) -> str:
    """Render the full Markdown report for one GAMEWEEK_REVIEW run."""
    sections = [
        "# Gameweek Review Report",
        "",
        "## Run",
        "",
        _run_table(learning_report_id, target_fpl_event_id, started_at, completed_at, status),
        "## Notable findings",
        "",
        _notable_findings_section(notable_findings),
        "## Recommendation",
        "",
        _recommendation_section(recommendation),
        "## Outcome",
        "",
        _outcome_section(outcome),
        "## Counterfactual comparisons",
        "",
        _counterfactuals_table(counterfactual_results),
        "## Failure attributions",
        "",
        _failure_attributions_table(failure_attributions),
        "## Learning signals",
        "",
        _learning_signals_table(learning_signals),
        "## Retraining trigger",
        "",
        _retraining_trigger_section(retraining),
    ]
    return "\n".join(sections)
