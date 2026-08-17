"""Unit tests for app.learning.report's pure composition/rendering logic (no DB/network).

``compose_notable_findings`` is the genuinely interesting pure decision here
(which of this run's already-computed structured facts are material enough
to surface) -- covered thoroughly below. ``render_gameweek_review_report_markdown``
gets a lighter smoke test (non-empty, contains expected values) since its own
correctness is really "does the table-building follow app.ingestion.report's
established style", already exercised end-to-end by
``tests/integration/test_gameweek_review_job.py``.
"""

from app.learning.report import (
    CounterfactualResultSummary,
    FailureAttributionSummary,
    LearningSignalSummary,
    NoteEffectivenessSummary,
    OutcomeSummary,
    RecommendationSummary,
    compose_notable_findings,
    render_gameweek_review_report_markdown,
)

# --- compose_notable_findings -------------------------------------------------------------------


def test_compose_notable_findings_all_sound_no_breach_is_empty():
    failure_attributions = [
        FailureAttributionSummary(
            reason_code="recommendation_was_sound", category="variance", severity=None, evidence={}
        )
    ]
    learning_signals = [
        LearningSignalSummary(
            signal_type="rolling_projection_mae",
            window_size_gameweeks=6,
            value=0.9,
            sample_count=10,
            threshold_breached=False,
        )
    ]
    assert compose_notable_findings(failure_attributions, learning_signals) == []


def test_compose_notable_findings_material_failure_is_included():
    failure_attributions = [
        FailureAttributionSummary(
            reason_code="chip_timing_suboptimal",
            category="optimization",
            severity=8.5,
            evidence={"delta": -8.5},
        )
    ]
    findings = compose_notable_findings(failure_attributions, [])
    assert findings == [
        {
            "type": "material_failure",
            "reason_code": "chip_timing_suboptimal",
            "category": "optimization",
            "severity": 8.5,
            "evidence": {"delta": -8.5},
        }
    ]


def test_compose_notable_findings_recommendation_was_sound_never_included_alongside_real_failures():
    failure_attributions = [
        FailureAttributionSummary(
            reason_code="recommendation_was_sound", category="variance", severity=None, evidence={}
        ),
        FailureAttributionSummary(
            reason_code="projection_overestimated",
            category="performance_model",
            severity=6.0,
            evidence={"player_id": "p1"},
        ),
    ]
    findings = compose_notable_findings(failure_attributions, [])
    assert len(findings) == 1
    assert findings[0]["reason_code"] == "projection_overestimated"


def test_compose_notable_findings_breached_signal_is_included():
    learning_signals = [
        LearningSignalSummary(
            signal_type="rolling_h2h_win_probability_calibration",
            window_size_gameweeks=8,
            value=0.35,
            sample_count=5,
            threshold_breached=True,
        )
    ]
    findings = compose_notable_findings([], learning_signals)
    assert findings == [
        {
            "type": "signal_breached",
            "signal_type": "rolling_h2h_win_probability_calibration",
            "value": 0.35,
            "sample_count": 5,
            "window_size_gameweeks": 8,
        }
    ]


def test_compose_notable_findings_non_breached_signal_is_excluded():
    learning_signals = [
        LearningSignalSummary(
            signal_type="rolling_chip_recommendation_value",
            window_size_gameweeks=20,
            value=3.0,
            sample_count=2,
            threshold_breached=False,
        )
    ]
    assert compose_notable_findings([], learning_signals) == []


def test_compose_notable_findings_orders_failures_before_signals():
    failure_attributions = [
        FailureAttributionSummary(
            reason_code="variance_unexplained", category="variance", severity=None, evidence={}
        )
    ]
    learning_signals = [
        LearningSignalSummary(
            signal_type="rolling_projection_mae",
            window_size_gameweeks=6,
            value=2.5,
            sample_count=12,
            threshold_breached=True,
        )
    ]
    findings = compose_notable_findings(failure_attributions, learning_signals)
    assert [f["type"] for f in findings] == ["material_failure", "signal_breached"]


def test_compose_notable_findings_note_effectiveness_negative_is_included():
    note_evaluations = [
        NoteEffectivenessSummary(
            player_id="p1",
            note_type="availability_risk",
            note_severity=0.65,
            projected_points_with_note=6.5,
            projected_points_without_note=10.0,
            actual_points=10.0,
            error_with_note=3.5,
            error_without_note=0.0,
            moved_closer_to_actual=False,
        )
    ]
    findings = compose_notable_findings([], [], note_evaluations=note_evaluations)
    assert findings == [
        {
            "type": "note_effectiveness_negative",
            "player_id": "p1",
            "note_type": "availability_risk",
            "note_severity": 0.65,
            "error_with_note": 3.5,
            "error_without_note": 0.0,
        }
    ]


def test_compose_notable_findings_note_effectiveness_positive_and_tie_are_excluded():
    note_evaluations = [
        NoteEffectivenessSummary(
            player_id="p1",
            note_type="availability_risk",
            note_severity=0.65,
            projected_points_with_note=6.5,
            projected_points_without_note=10.0,
            actual_points=6.0,
            error_with_note=0.5,
            error_without_note=4.0,
            moved_closer_to_actual=True,
        ),
        NoteEffectivenessSummary(
            player_id="p2",
            note_type="role_boost",
            note_severity=1.15,
            projected_points_with_note=11.5,
            projected_points_without_note=10.0,
            actual_points=10.75,
            error_with_note=0.75,
            error_without_note=0.75,
            moved_closer_to_actual=None,
        ),
    ]
    assert compose_notable_findings([], [], note_evaluations=note_evaluations) == []


def test_compose_notable_findings_no_note_evaluations_argument_defaults_to_no_op():
    assert compose_notable_findings([], []) == []


# --- render_gameweek_review_report_markdown (smoke test) ----------------------------------------


def test_render_gameweek_review_report_markdown_contains_expected_values():
    from datetime import UTC, datetime

    started_at = datetime(2026, 3, 1, tzinfo=UTC)
    completed_at = datetime(2026, 3, 1, 1, tzinfo=UTC)
    recommendation = RecommendationSummary(
        confidence="high",
        component_summaries=["joint_squad_and_xi: xi_projected_points=65.00"],
        known_risks=[],
    )
    outcome = OutcomeSummary(
        actual_xi_points=72,
        actual_captain_points=16,
        actual_bench_points=6,
        h2h_result="win",
        actual_chip_played="bboost",
        recommendation_net_gain=6.0,
    )
    counterfactual_results = [
        CounterfactualResultSummary(
            comparison_type="chip_vs_hold",
            baseline_points=0.0,
            alternative_points=6.0,
            delta=6.0,
            is_value_adding=True,
        )
    ]
    failure_attributions = [
        FailureAttributionSummary(
            reason_code="recommendation_was_sound", category="variance", severity=None, evidence={}
        )
    ]
    learning_signals = [
        LearningSignalSummary(
            signal_type="rolling_projection_mae",
            window_size_gameweeks=6,
            value=1.1,
            sample_count=11,
            threshold_breached=False,
        )
    ]
    notable_findings: list[dict] = []
    note_evaluations = [
        NoteEffectivenessSummary(
            player_id="p1",
            note_type="availability_risk",
            note_severity=0.65,
            projected_points_with_note=6.5,
            projected_points_without_note=10.0,
            actual_points=6.0,
            error_with_note=0.5,
            error_without_note=4.0,
            moved_closer_to_actual=True,
        )
    ]

    markdown = render_gameweek_review_report_markdown(
        learning_report_id="11111111-1111-1111-1111-111111111111",
        target_fpl_event_id=20,
        started_at=started_at,
        completed_at=completed_at,
        status="succeeded",
        recommendation=recommendation,
        outcome=outcome,
        counterfactual_results=counterfactual_results,
        failure_attributions=failure_attributions,
        learning_signals=learning_signals,
        notable_findings=notable_findings,
        note_evaluations=note_evaluations,
    )

    assert markdown  # non-empty
    assert "GW20" in markdown
    assert "high" in markdown
    assert "chip_vs_hold" in markdown
    assert "recommendation_was_sound" in markdown
    assert "rolling_projection_mae" in markdown
    assert "no material findings" in markdown.lower()
    assert "Note effectiveness" in markdown
    assert "availability_risk" in markdown


def test_render_gameweek_review_report_markdown_no_note_evaluations_shows_placeholder():
    from datetime import UTC, datetime

    started_at = datetime(2026, 3, 1, tzinfo=UTC)
    completed_at = datetime(2026, 3, 1, 1, tzinfo=UTC)
    recommendation = RecommendationSummary(confidence="high", component_summaries=[], known_risks=[])
    outcome = OutcomeSummary(
        actual_xi_points=None,
        actual_captain_points=None,
        actual_bench_points=None,
        h2h_result=None,
        actual_chip_played=None,
        recommendation_net_gain=None,
    )

    markdown = render_gameweek_review_report_markdown(
        learning_report_id="11111111-1111-1111-1111-111111111111",
        target_fpl_event_id=20,
        started_at=started_at,
        completed_at=completed_at,
        status="succeeded",
        recommendation=recommendation,
        outcome=outcome,
        counterfactual_results=[],
        failure_attributions=[],
        learning_signals=[],
        notable_findings=[],
    )

    assert "no active availability-risk/role-boost notes" in markdown.lower()
