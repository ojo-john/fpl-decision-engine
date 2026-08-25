"""GAMEWEEK_REVIEW job: orchestrate Layers 2-3 end to end into one persisted ``learning_reports`` row.

Milestone 7 Layer 4 (Recursive Learning Loop: GAMEWEEK_REVIEW orchestration).
Unlike ``RECOMMENDATION_GENERATION`` (Layer 1, deliberately a pure
aggregator that reads already-persisted rows and does no new computation),
this handler genuinely IS an orchestrator: its whole job is to SEQUENCE
Layer 2's ``app.learning.outcome_ingestion.ingest_outcome_for_audit`` and
Layer 3's ``app.learning.attribution.attribute_failures_for_outcome`` /
``app.learning.signals``' 4 ``compute_*``/``persist_learning_signal``
functions -- functions that are each already independently tested -- and
compose their outputs into one readable report. It contains almost no new
computation of its own beyond ``app.learning.report``'s pure
``compose_notable_findings`` composition and Markdown rendering.

Input resolution: ``ctx.parameters["target_fpl_event_id"]`` is REQUIRED (not
auto-resolved) -- unlike the forward-looking optimizer jobs, "review the most
recent finished gameweek" has no single unambiguous meaning (which finished
gameweek? the immediately-previous one? one several weeks back whose actuals
just got corrected?), so a caller must always say explicitly which gameweek
to review. ``ctx.parameters["season_id"]`` is optional, resolved via
``Season.is_current`` when omitted -- mirrors
``app.jobs.handlers.recommendation_generation``'s own season-resolution
precedent.

Two hard-refusal input checks, BOTH before any row is created (mirrors every
other job handler's own "a clear input problem never pollutes the audit
trail" discipline):

1. ``GAMEWEEK_NOT_FINISHED`` -- the resolved gameweek's own ``finished`` flag
   is not ``True``. This is genuinely the SAME underlying refusal
   ``ingest_outcome_for_audit`` itself would raise (``GameweekNotFinishedError``)
   once called -- checked here too, earlier, at the job's own entry point,
   purely so a caller gets a clean, job-level ``FAILED`` result (with no
   ``learning_reports`` row at all) instead of a mid-run crash after a
   ``RUNNING`` row was already created.
2. ``NO_AUTHORITATIVE_RECOMMENDATION`` -- no ``is_authoritative=True``
   ``recommendation_audits`` row exists for this exact ``(season_id,
   gameweek_id)``. Nothing to review without one.

RUNNING-row discipline: mirrors ``ChipSimulationJobRunner``/
``H2HSimulationJobRunner``'s exact create-early-RUNNING-row precedent -- the
``learning_reports`` row is created immediately after the two input checks
above pass, BEFORE any of Layer 2/3's own work runs, so a crash mid-review
(e.g. one of the ``compute_rolling_*`` calls raising unexpectedly) still
leaves a terminal ``FAILED`` row behind via ``_finalize_failed``, never a
silently-dangling ``RUNNING`` one.

Rerun behavior: ``learning_reports`` is INSERT-ONLY per run (see that
model's own docstring) -- a second GAMEWEEK_REVIEW call for the same
gameweek always creates a FRESH ``learning_reports`` row, never updates a
prior one. This composes correctly with Layer 2/3's own already-established
idempotency: ``ingest_outcome_for_audit`` UPSERTS the same
``recommendation_outcomes`` row in place (so ``recommendation_outcome_id``
on two different ``learning_reports`` rows for the same gameweek can
legitimately point at the SAME outcome row, now carrying whatever corrected
data triggered the rerun), ``attribute_failures_for_outcome`` deletes and
re-persists ``failure_attributions`` scoped to that same outcome id, and
each ``compute_rolling_*`` call always inserts a fresh, distinct
``learning_signals`` row (never upserted -- "each computation is its own
recordable event", see that module's own docstring). Nothing here needs to
special-case a rerun; each layer's own existing convention already produces
the right outcome when simply called again.

Milestone 7 Layer 5 (Recursive Learning Loop: Challenger Training/Shadow-Mode
Evaluation/Promotion Gates/Rollback) adds one small, ADDITIVE step after the
rolling learning signals above: ``app.modeling.shadow_evaluation
.compute_shadow_challenger_evaluation`` scores the current shadow challenger
(if any) against the current champion for this now-finished gameweek,
persisting a ``model_challenger_evaluations`` row. This is a no-op (returns
``None``, nothing added to the report) whenever no shadow challenger
currently exists -- the common case, and the only state most environments
will ever be in. See that function's own module docstring for why this is
computed retroactively here rather than during live optimization. This job
does NOT itself decide whether to promote/reject/roll back -- that is
``app.modeling.promotion``'s job, invoked separately (Milestone 7 Layer 6's
concern to wire into an automatic trigger; not done by this handler).

Milestone 7 Layer 6 (Recursive Learning Loop: BACKTEST job + the
GAMEWEEK_REVIEW -> MODEL_TRAINING retraining-trigger wiring), REVISED
2026-08-25: after the 4 rolling learning signals above are persisted, if
THIS run's own ``ROLLING_PROJECTION_MAE`` signal came back with
``threshold_breached=True`` (a genuine, sustained degradation pattern -- see
``app.learning.signals``' own module docstring for the exact sustained-
window semantics), a real push notification is sent
(``app.notifications.push_sender.send_push_to_all``) asking a human to
manually retrain via the **Train (production database)** GitHub Actions
workflow. ``ROLLING_CHIP_RECOMMENDATION_VALUE``'s own ``threshold_breached``
is NEVER consulted here -- that signal is permanently informational-only per
its own module docstring.

This step ORIGINALLY ran ``MODEL_TRAINING`` in-process via
``app.jobs.service.execute_job`` and, on a fresh ``SHADOW_CHALLENGER``,
immediately ran a promotion-gate check
(``app.modeling.promotion.evaluate_promotion``/``decide_and_record_promotion``)
too. Real incident (2026-08-25): whichever server runs ``GAMEWEEK_REVIEW`` in
production is Render's own free-tier web service, and running
``MODEL_TRAINING`` there OOM-crashed the live API mid-training (confirmed via
the exact same real production retraining attempt this session spent
extensively debugging -- see ``.github/workflows/train-production.yml``'s own
docstring for the full incident and why real retraining now always needs a
deliberate human decision about where to run it). Automatically retraining
from inside a request-serving process was never safe once training grew
past what that process's own resources could handle -- alerting a human
instead removes that risk entirely, at the cost of retraining no longer
being fully automatic. ``evaluate_promotion``/``decide_and_record_promotion``
remain real, valid, reusable functions (unrelated call sites can still use
them) -- only this specific automatic in-process invocation was removed.

Milestone 7 extension (Recursive Learning Loop: note-severity outcome tracking) adds one more
small, ADDITIVE step, same failure-isolation shape as the price-signal recalibration step above:
``self._track_note_effectiveness`` calls ``app.learning.note_effectiveness
.evaluate_note_effectiveness_for_outcome`` to find every ``player_availability_risk_notes``/
``player_role_notes`` row that was actually applied to this gameweek's recommended-squad
projection, and records whether applying it moved the projection closer to or further from the
real outcome -- see that module's own docstring for the full derivation and why this can only
ever be informational tracking (no automated recalibration), not a hard gate. NEVER raises;
folds any failure into a plain error string surfaced only in ``summary_json`` (mirrors
``PriceSignalRecalibrationSummary.error``), never this run's own ``learning_reports.run_status``.

Failure-isolation boundary: this entire retraining-alert step is wrapped so
that ANY failure inside it (a failed push send) is caught and folded into
``app.learning.report.RetrainingTriggerSummary.error`` instead of ever
reaching this handler's own top-level exception handler -- GAMEWEEK_REVIEW's
own primary work (outcome ingestion, failure attribution, the 4 signals) has
ALREADY fully succeeded by the time this step runs, and a failed alert is a
completely different, non-fatal event that must never flip this run's own
``learning_reports.run_status`` to ``FAILED``. See
``GameweekReviewJobRunner._alert_on_mae_breach``'s own docstring for exactly
how this is implemented.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.jobs.registry import register_handler
from app.jobs.runner import JobContext, JobResult, JobRunner
from app.learning.attribution import attribute_failures_for_outcome
from app.learning.note_effectiveness import evaluate_note_effectiveness_for_outcome
from app.learning.outcome_ingestion import ingest_outcome_for_audit
from app.learning.report import (
    CounterfactualResultSummary,
    FailureAttributionSummary,
    LearningSignalSummary,
    NoteEffectivenessSummary,
    OutcomeSummary,
    RecommendationSummary,
    RetrainingTriggerSummary,
    compose_notable_findings,
    render_gameweek_review_report_markdown,
)
from app.learning.signals import (
    compute_failure_reason_concentration,
    compute_rolling_chip_recommendation_value,
    compute_rolling_h2h_calibration,
    compute_rolling_projection_mae,
    persist_learning_signal,
)
from app.modeling.shadow_evaluation import compute_shadow_challenger_evaluation
from app.notifications.push_sender import send_push_to_all
from app.players.price_signal import DEFAULT_MIN_ABSOLUTE_NET_TRANSFERS, DEFAULT_MOMENTUM_RATIO_THRESHOLD
from app.players.price_signal_calibration import (
    PlayerPriceChangeInput,
    PlayerPriceObservationInput,
    build_calibration_samples,
    calibrate_thresholds,
)
from packages.database.models.chip_optimization_run import ChipOptimizationRun
from packages.database.models.counterfactual_result import CounterfactualResult
from packages.database.models.gameweek import Gameweek
from packages.database.models.h2h_simulation_run import H2HSimulationRun
from packages.database.models.learning_report import LearningReport
from packages.database.models.note_effectiveness_evaluation import NoteEffectivenessEvaluation
from packages.database.models.optimization_run import OptimizationRun
from packages.database.models.player_price_history import PlayerPriceHistory
from packages.database.models.player_price_signal_observation import PlayerPriceSignalObservation
from packages.database.models.price_signal_calibration import PriceSignalCalibration
from packages.database.models.recommendation_audit import RecommendationAudit
from packages.database.models.season import Season
from packages.database.models.transfer_optimization_run import TransferOptimizationRun
from packages.database.session import SessionLocal
from packages.domain.jobs import JobStatus, JobType
from packages.domain.learning import GameweekReviewRunStatus, LearningSignalType

logger = logging.getLogger(__name__)


class _InputResolutionError(Exception):
    """Raised for a clear, user-facing input problem, always caught before any row is created."""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


def _parse_uuid_parameter(parameters: dict | None, key: str) -> uuid.UUID:
    raw = (parameters or {}).get(key)
    try:
        return uuid.UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        raise _InputResolutionError(
            f"INVALID_{key.upper()}", f"parameters.{key} is not a valid UUID."
        ) from None


@dataclass(frozen=True)
class _ResolvedInputs:
    season_id: uuid.UUID
    gameweek: Gameweek
    audit: RecommendationAudit


@dataclass(frozen=True)
class PriceSignalRecalibrationSummary:
    """What happened, if anything, when this run checked whether enough real price-change
    evidence has accumulated to recalibrate ``app.players.price_signal``'s thresholds.

    ``triggered=False`` (every other field ``None``) covers both "not enough samples yet"
    and "recalibration itself failed" -- see ``error`` to distinguish the latter. Mirrors
    ``RetrainingTriggerSummary``'s own shape/naming for consistency.
    """

    triggered: bool
    sample_size: int | None = None
    ratio_threshold: str | None = None
    absolute_floor: int | None = None
    accuracy: str | None = None
    error: str | None = None


def _resolve_inputs(session: Session, parameters: dict | None) -> _ResolvedInputs:
    raw_target_event = (parameters or {}).get("target_fpl_event_id")
    if raw_target_event is None:
        raise _InputResolutionError(
            "MISSING_TARGET_FPL_EVENT_ID", "parameters.target_fpl_event_id is required."
        )
    try:
        target_fpl_event_id = int(raw_target_event)
    except (TypeError, ValueError):
        raise _InputResolutionError(
            "INVALID_TARGET_FPL_EVENT_ID", "parameters.target_fpl_event_id must be an integer."
        ) from None

    raw_season_id = (parameters or {}).get("season_id")
    season_id: uuid.UUID
    if raw_season_id is not None:
        season_id = _parse_uuid_parameter(parameters, "season_id")
    else:
        resolved_season_id = session.execute(
            select(Season.id).where(Season.is_current.is_(True))
        ).scalar_one_or_none()
        if resolved_season_id is None:
            raise _InputResolutionError("NO_CURRENT_SEASON", "No current season found.")
        season_id = resolved_season_id

    gameweek = session.execute(
        select(Gameweek).where(Gameweek.season_id == season_id, Gameweek.fpl_event_id == target_fpl_event_id)
    ).scalar_one_or_none()
    if gameweek is None:
        raise _InputResolutionError(
            "GAMEWEEK_NOT_FOUND", f"No gameweek found for fpl_event_id={target_fpl_event_id}."
        )
    # Same underlying refusal ingest_outcome_for_audit itself would raise -- checked here, earlier,
    # for a clean job-level failure before any row exists. See module docstring.
    if not gameweek.finished:
        raise _InputResolutionError(
            "GAMEWEEK_NOT_FINISHED",
            f"Gameweek {target_fpl_event_id} is not yet finished; it cannot be reviewed yet.",
        )

    audit = session.execute(
        select(RecommendationAudit).where(
            RecommendationAudit.season_id == season_id,
            RecommendationAudit.target_gameweek_id == gameweek.id,
            RecommendationAudit.is_authoritative.is_(True),
        )
    ).scalar_one_or_none()
    if audit is None:
        raise _InputResolutionError(
            "NO_AUTHORITATIVE_RECOMMENDATION",
            f"No authoritative recommendation_audits row exists for GW{target_fpl_event_id}; "
            "nothing to review.",
        )

    return _ResolvedInputs(season_id=season_id, gameweek=gameweek, audit=audit)


def _find_projection_mae_signal(
    signal_summaries: list[LearningSignalSummary],
) -> LearningSignalSummary | None:
    """Pure lookup -- the one ``LearningSignalSummary`` (if any) for
    ``LearningSignalType.ROLLING_PROJECTION_MAE`` among this run's own 4 just-computed signals.

    The actual retraining-trigger DECISION is just reading that signal's own
    ``threshold_breached`` boolean directly (see module docstring) -- there is
    no further decision logic to factor out beyond this lookup, which is why
    this is the one piece of Layer 6 logic broken out as a small, directly
    unit-testable pure function.
    """
    return next(
        (s for s in signal_summaries if s.signal_type == LearningSignalType.ROLLING_PROJECTION_MAE.value),
        None,
    )


# --- "What was recommended" summary -- deliberately duplicated (not imported) from
# --- apps.api.routers.recommendations's own private per-component summary helpers: that module
# --- lives in the API layer, importing from it here would invert this codebase's own app->apps
# --- layering, and each summary is a one-line read, not worth a shared module for. -----------------


def _build_recommendation_summary(session: Session, audit: RecommendationAudit) -> RecommendationSummary:
    summaries: list[str] = []
    if audit.optimization_run_id is not None:
        run = session.get(OptimizationRun, audit.optimization_run_id)
        if run is not None:
            summaries.append(
                f"{run.recommendation_type.value}: xi_projected_points={run.xi_projected_points}"
            )
    if audit.transfer_optimization_run_id is not None:
        transfer_run = session.get(TransferOptimizationRun, audit.transfer_optimization_run_id)
        if transfer_run is not None:
            summaries.append(
                f"{transfer_run.recommended_transfers_count} transfer(s) recommended, "
                f"net gain {transfer_run.recommended_net_horizon_points_gain}"
            )
    if audit.h2h_simulation_run_id is not None:
        h2h_run = session.get(H2HSimulationRun, audit.h2h_simulation_run_id)
        if h2h_run is not None:
            summaries.append(f"H2H win_probability={h2h_run.win_probability}")
    if audit.chip_optimization_run_id is not None:
        chip_run = session.get(ChipOptimizationRun, audit.chip_optimization_run_id)
        if chip_run is not None:
            summaries.append(f"Chip evaluation completed for GW{chip_run.target_fpl_event_id}")

    return RecommendationSummary(
        confidence=audit.confidence.value,
        component_summaries=summaries,
        known_risks=list(audit.known_risks_json or []),
    )


@register_handler(JobType.GAMEWEEK_REVIEW)
class GameweekReviewJobRunner(JobRunner):
    """Sequences Layer 2's outcome ingestion and Layer 3's failure attribution/rolling learning
    signals for one already-finished gameweek's authoritative recommendation, persisting one
    ``learning_reports`` row. See module docstring for the full orchestration order.
    """

    def run(self, ctx: JobContext) -> JobResult:
        session = SessionLocal()
        report_row: LearningReport | None = None
        try:
            try:
                resolved = _resolve_inputs(session, ctx.parameters)
            except _InputResolutionError as exc:
                return JobResult(
                    status=JobStatus.FAILED, output=None, error_code=exc.error_code, error_message=str(exc)
                )

            season_id = resolved.season_id
            gameweek = resolved.gameweek
            audit = resolved.audit
            target_fpl_event_id = gameweek.fpl_event_id

            try:
                started_at = datetime.now(UTC)
                report_row = LearningReport(
                    job_id=uuid.UUID(ctx.job_id),
                    season_id=season_id,
                    gameweek_id=gameweek.id,
                    run_status=GameweekReviewRunStatus.RUNNING,
                    started_at=started_at,
                )
                session.add(report_row)
                session.commit()
                session.refresh(report_row)

                # --- Layer 2: outcome ingestion ---
                outcome = ingest_outcome_for_audit(session, audit.id)

                # --- Layer 3: failure attribution (already persisted internally, not double-persisted) ---
                attributions = attribute_failures_for_outcome(session, outcome.id)

                # --- Note-severity outcome tracking: additive, never raises -- see module
                # docstring and _track_note_effectiveness's own docstring for the failure-
                # isolation contract (mirrors _maybe_recalibrate_price_signal below).
                note_effectiveness_rows, note_effectiveness_error = self._track_note_effectiveness(
                    session, outcome.id
                )

                # --- Layer 3: rolling learning signals, as-of this reviewed gameweek ---
                signal_data = [
                    compute_rolling_projection_mae(session, season_id, gameweek.id),
                    compute_rolling_h2h_calibration(session, season_id, gameweek.id),
                    compute_rolling_chip_recommendation_value(session, season_id, gameweek.id),
                    compute_failure_reason_concentration(session, season_id, gameweek.id),
                ]
                persisted_signals = [
                    persist_learning_signal(session, season_id, gameweek.id, signal) for signal in signal_data
                ]

                # --- Layer 5: retroactive shadow-challenger scoring, additive -- see
                # app.modeling.shadow_evaluation's own module docstring for why this is computed
                # retroactively (against this now-finished gameweek's real player_features actuals)
                # rather than piggybacked on a live projection loop. A no-op (returns None) whenever
                # no shadow challenger currently exists -- the common case.
                shadow_evaluation = compute_shadow_challenger_evaluation(session, season_id, gameweek.id)

                counterfactual_results = (
                    session.execute(
                        select(CounterfactualResult).where(
                            CounterfactualResult.recommendation_outcome_id == outcome.id
                        )
                    )
                    .scalars()
                    .all()
                )

                failure_summaries = [
                    FailureAttributionSummary(
                        reason_code=attribution.reason_code.value,
                        category=attribution.category.value,
                        severity=(float(attribution.severity) if attribution.severity is not None else None),
                        evidence=attribution.evidence_json,
                    )
                    for attribution in attributions
                ]
                signal_summaries = [
                    LearningSignalSummary(
                        signal_type=signal.signal_type.value,
                        window_size_gameweeks=signal.window_size_gameweeks,
                        value=float(signal.value),
                        sample_count=signal.sample_count,
                        threshold_breached=signal.threshold_breached,
                    )
                    for signal in persisted_signals
                ]
                counterfactual_summaries = [
                    CounterfactualResultSummary(
                        comparison_type=result.comparison_type.value,
                        baseline_points=float(result.baseline_points),
                        alternative_points=float(result.alternative_points),
                        delta=float(result.delta),
                        is_value_adding=result.is_value_adding,
                    )
                    for result in counterfactual_results
                ]
                note_effectiveness_summaries = [
                    NoteEffectivenessSummary(
                        player_id=str(row.player_id),
                        note_type=row.note_type,
                        note_severity=float(row.note_severity),
                        projected_points_with_note=float(row.projected_points_with_note),
                        projected_points_without_note=float(row.projected_points_without_note),
                        actual_points=float(row.actual_points),
                        error_with_note=float(row.error_with_note),
                        error_without_note=float(row.error_without_note),
                        moved_closer_to_actual=row.moved_closer_to_actual,
                    )
                    for row in note_effectiveness_rows
                ]

                # --- Layer 6: retraining-alert wiring -- reacts ONLY to ROLLING_PROJECTION_MAE's
                # own threshold_breached (never ROLLING_CHIP_RECOMMENDATION_VALUE's, which is
                # permanently informational-only). See module docstring for the full failure-
                # isolation boundary _alert_on_mae_breach itself enforces.
                mae_signal_summary = _find_projection_mae_signal(signal_summaries)
                retraining_summary = RetrainingTriggerSummary(triggered=False)
                if mae_signal_summary is not None and mae_signal_summary.threshold_breached:
                    retraining_summary = self._alert_on_mae_breach(session)

                # --- price-signal recalibration: independent of the MAE-breach retraining trigger
                # above -- runs every finished-gameweek review, since price-change evidence
                # accumulates regardless of whether the projection model itself is degrading. See
                # _maybe_recalibrate_price_signal's own docstring for the failure-isolation contract.
                price_signal_summary = self._maybe_recalibrate_price_signal(session, season_id)

                notable_findings = compose_notable_findings(
                    failure_summaries, signal_summaries, retraining_summary, note_effectiveness_summaries
                )
                recommendation_summary = _build_recommendation_summary(session, audit)
                outcome_summary = OutcomeSummary(
                    actual_xi_points=outcome.actual_xi_points,
                    actual_captain_points=outcome.actual_captain_points,
                    actual_bench_points=outcome.actual_bench_points,
                    h2h_result=outcome.h2h_result,
                    actual_chip_played=outcome.actual_chip_played,
                    recommendation_net_gain=(
                        float(outcome.recommendation_net_gain)
                        if outcome.recommendation_net_gain is not None
                        else None
                    ),
                )

                completed_at = datetime.now(UTC)
                report_markdown = render_gameweek_review_report_markdown(
                    learning_report_id=str(report_row.id),
                    target_fpl_event_id=target_fpl_event_id,
                    started_at=started_at,
                    completed_at=completed_at,
                    status=GameweekReviewRunStatus.SUCCEEDED.value,
                    recommendation=recommendation_summary,
                    outcome=outcome_summary,
                    counterfactual_results=counterfactual_summaries,
                    failure_attributions=failure_summaries,
                    learning_signals=signal_summaries,
                    notable_findings=notable_findings,
                    retraining=retraining_summary,
                    note_evaluations=note_effectiveness_summaries,
                )

                learning_signals_computed = {
                    signal.signal_type: {
                        "value": signal.value,
                        "sample_count": signal.sample_count,
                        "threshold_breached": signal.threshold_breached,
                        "window_size_gameweeks": signal.window_size_gameweeks,
                    }
                    for signal in signal_summaries
                }
                summary = {
                    "target_fpl_event_id": target_fpl_event_id,
                    "recommendation_confidence": audit.confidence.value,
                    "counterfactual_comparison_count": len(counterfactual_summaries),
                    "failure_attribution_count": len(failure_summaries),
                    "notable_finding_count": len(notable_findings),
                    "shadow_challenger_evaluation": (
                        {
                            "challenger_model_version_id": str(shadow_evaluation.challenger_model_version_id),
                            "champion_model_version_id": str(shadow_evaluation.champion_model_version_id),
                            "challenger_mae": float(shadow_evaluation.challenger_mae)
                            if shadow_evaluation.challenger_mae is not None
                            else None,
                            "champion_mae": float(shadow_evaluation.champion_mae)
                            if shadow_evaluation.champion_mae is not None
                            else None,
                            "n_observations": shadow_evaluation.n_observations,
                        }
                        if shadow_evaluation is not None
                        else None
                    ),
                    "retraining_triggered": retraining_summary.triggered,
                    "retraining_alert_sent": retraining_summary.alert_sent,
                    "price_signal_recalibration_triggered": price_signal_summary.triggered,
                    "price_signal_recalibration_sample_size": price_signal_summary.sample_size,
                    "price_signal_new_ratio_threshold": price_signal_summary.ratio_threshold,
                    "price_signal_new_absolute_floor": price_signal_summary.absolute_floor,
                    "note_effectiveness_count": len(note_effectiveness_summaries),
                    "note_effectiveness_negative_count": sum(
                        1
                        for evaluation in note_effectiveness_summaries
                        if evaluation.moved_closer_to_actual is False
                    ),
                    "note_effectiveness_error": note_effectiveness_error,
                }

                report_row.recommendation_outcome_id = outcome.id
                report_row.failure_attribution_count = len(attributions)
                report_row.learning_signals_computed_json = learning_signals_computed
                report_row.report_markdown = report_markdown
                report_row.summary_json = summary
                report_row.notable_findings_json = notable_findings
                report_row.retraining_triggered = retraining_summary.triggered
                # No longer set: a real MODEL_TRAINING job never actually runs from this step
                # anymore, only an alert -- see RetrainingTriggerSummary's own docstring.
                report_row.retraining_job_id = None
                report_row.run_status = GameweekReviewRunStatus.SUCCEEDED
                report_row.completed_at = completed_at
                session.commit()

                return JobResult(
                    status=JobStatus.SUCCEEDED,
                    output={
                        "learning_report_id": str(report_row.id),
                        "target_fpl_event_id": target_fpl_event_id,
                        "recommendation_outcome_id": str(outcome.id),
                        "failure_attribution_count": len(attributions),
                        "notable_finding_count": len(notable_findings),
                        "shadow_challenger_evaluation_id": (
                            str(shadow_evaluation.id) if shadow_evaluation is not None else None
                        ),
                        "retraining_triggered": retraining_summary.triggered,
                        "retraining_alert_sent": retraining_summary.alert_sent,
                        "price_signal_recalibration_triggered": price_signal_summary.triggered,
                        "price_signal_recalibration_sample_size": price_signal_summary.sample_size,
                        "price_signal_new_ratio_threshold": price_signal_summary.ratio_threshold,
                        "price_signal_new_absolute_floor": price_signal_summary.absolute_floor,
                        "note_effectiveness_count": len(note_effectiveness_summaries),
                    },
                    error_code=None,
                    error_message=None,
                )
            except Exception as exc:  # noqa: BLE001 - the run row must always reach a terminal status
                session.rollback()
                self._finalize_failed(session, report_row)
                return JobResult(
                    status=JobStatus.FAILED,
                    output={"learning_report_id": str(report_row.id)} if report_row is not None else None,
                    error_code="GAMEWEEK_REVIEW_FAILED",
                    error_message=str(exc),
                )
        finally:
            session.close()

    def _finalize_failed(self, session: Session, report_row: LearningReport | None) -> None:
        if report_row is None or report_row.id is None:
            return
        try:
            fresh = session.get(LearningReport, report_row.id)
            if fresh is not None and fresh.run_status == GameweekReviewRunStatus.RUNNING:
                fresh.run_status = GameweekReviewRunStatus.FAILED
                fresh.completed_at = datetime.now(UTC)
                session.commit()
        except Exception:  # noqa: BLE001 - best-effort finalization only
            session.rollback()

    @staticmethod
    def _alert_on_mae_breach(session: Session) -> RetrainingTriggerSummary:
        """Send a real push notification asking a human to manually retrain, via the **Train
        (production database)** GitHub Actions workflow.

        Revised 2026-08-25: this used to run ``MODEL_TRAINING`` in-process (via
        ``app.jobs.service.execute_job`` on a dedicated session) and, on a fresh
        ``SHADOW_CHALLENGER``, immediately ran a promotion-gate check
        (``app.modeling.promotion.evaluate_promotion``/``decide_and_record_promotion``). Real
        incident: whichever server runs GAMEWEEK_REVIEW in production is Render's own free-tier
        web service, and running MODEL_TRAINING there OOM-crashed the live API mid-training (the
        exact same real retraining attempt this session spent hours debugging -- see
        ``.github/workflows/train-production.yml``'s own docstring for the full incident).
        Automatic in-process retraining was never safe once training grew past what a
        request-serving process's own resources could handle -- alerting a human instead removes
        that risk entirely, at the cost of retraining no longer being fully automatic.

        NEVER raises -- a failed push send is caught and folded into the returned
        ``RetrainingTriggerSummary.error`` instead, so a notification failure can never fail
        GAMEWEEK_REVIEW's own primary success. This is the concrete failure-isolation boundary the
        module docstring promises -- the caller (``run``) still keeps its own top-level
        ``except Exception`` as defense in depth, but should never actually need it because of
        this.
        """
        try:
            send_push_to_all(
                session,
                title="Model accuracy has degraded",
                body=(
                    "Sustained projection MAE increase detected. Consider retraining via the "
                    "Train (production database) GitHub Actions workflow."
                ),
            )
            return RetrainingTriggerSummary(triggered=True, alert_sent=True)
        except Exception as exc:  # noqa: BLE001 - see this method's own docstring: never propagates
            session.rollback()
            return RetrainingTriggerSummary(triggered=True, alert_sent=False, error=str(exc))

    def _maybe_recalibrate_price_signal(
        self, session: Session, season_id: uuid.UUID
    ) -> PriceSignalRecalibrationSummary:
        """Recalibrate ``app.players.price_signal``'s thresholds if enough real evidence exists.

        NEVER raises -- mirrors ``_alert_on_mae_breach``'s own
        failure-isolation contract exactly (see this module's docstring):
        this runs after GAMEWEEK_REVIEW's own primary work has already
        succeeded, so a recalibration failure must never flip this run's
        ``learning_reports.run_status`` to ``FAILED``. Uses this handler's
        own ``session`` (plain reads + a pure computation + one conditional
        write) rather than a nested job -- there is no separate JobType for
        this, it is not a user-triggerable action on its own.

        A no-op (``triggered=False``) whenever fewer than
        ``price_signal_calibration.MIN_CALIBRATION_SAMPLE_SIZE`` (signal,
        outcome) pairs exist yet for this season -- the common case for most
        of a season, and always true pre-season/early-season. See
        ``app.players.price_signal_calibration``'s own module docstring for
        why this data literally cannot exist until real gameweeks have been
        played.
        """
        try:
            observation_rows = session.execute(
                select(
                    PlayerPriceSignalObservation.player_id,
                    PlayerPriceSignalObservation.observed_at,
                    PlayerPriceSignalObservation.transfers_in_event,
                    PlayerPriceSignalObservation.transfers_out_event,
                    PlayerPriceSignalObservation.selected_by_percent,
                    PlayerPriceSignalObservation.total_players,
                ).where(PlayerPriceSignalObservation.season_id == season_id)
            ).all()
            change_rows = session.execute(
                select(PlayerPriceHistory.player_id, PlayerPriceHistory.observed_at).where(
                    PlayerPriceHistory.season_id == season_id
                )
            ).all()

            observations = [
                PlayerPriceObservationInput(
                    player_id=row.player_id,
                    observed_at=row.observed_at,
                    transfers_in_event=row.transfers_in_event,
                    transfers_out_event=row.transfers_out_event,
                    selected_by_percent=row.selected_by_percent,
                    total_players=row.total_players,
                )
                for row in observation_rows
            ]
            price_changes = [
                PlayerPriceChangeInput(player_id=player_id, observed_at=observed_at)
                for player_id, observed_at in change_rows
            ]

            samples = build_calibration_samples(observations, price_changes)

            active_calibration = session.execute(
                select(PriceSignalCalibration).where(PriceSignalCalibration.is_active.is_(True))
            ).scalar_one_or_none()
            current_ratio_threshold = (
                active_calibration.ratio_threshold
                if active_calibration is not None
                else DEFAULT_MOMENTUM_RATIO_THRESHOLD
            )
            current_absolute_floor = (
                active_calibration.absolute_floor
                if active_calibration is not None
                else DEFAULT_MIN_ABSOLUTE_NET_TRANSFERS
            )

            result = calibrate_thresholds(
                samples,
                current_ratio_threshold=current_ratio_threshold,
                current_absolute_floor=current_absolute_floor,
            )
            if result is None:
                logger.info(
                    "gameweek_review.price_signal_recalibration_skipped",
                    extra={"season_id": str(season_id), "sample_size": len(samples)},
                )
                return PriceSignalRecalibrationSummary(triggered=False)

            session.execute(
                update(PriceSignalCalibration)
                .where(PriceSignalCalibration.is_active.is_(True))
                .values(is_active=False)
            )
            session.add(
                PriceSignalCalibration(
                    ratio_threshold=result.ratio_threshold,
                    absolute_floor=result.absolute_floor,
                    sample_size=result.sample_size,
                    accuracy=result.accuracy,
                    is_active=True,
                    calibrated_at=datetime.now(UTC),
                )
            )
            session.commit()

            logger.info(
                "gameweek_review.price_signal_recalibrated",
                extra={
                    "season_id": str(season_id),
                    "sample_size": result.sample_size,
                    "previous_ratio_threshold": str(current_ratio_threshold),
                    "previous_absolute_floor": current_absolute_floor,
                    "new_ratio_threshold": str(result.ratio_threshold),
                    "new_absolute_floor": result.absolute_floor,
                    "accuracy": str(result.accuracy),
                },
            )
            return PriceSignalRecalibrationSummary(
                triggered=True,
                sample_size=result.sample_size,
                ratio_threshold=str(result.ratio_threshold),
                absolute_floor=result.absolute_floor,
                accuracy=str(result.accuracy),
            )
        except Exception as exc:  # noqa: BLE001 - never propagates, see this method's own docstring
            session.rollback()
            logger.warning(
                "gameweek_review.price_signal_recalibration_failed",
                extra={"season_id": str(season_id), "error": str(exc)},
            )
            return PriceSignalRecalibrationSummary(triggered=False, error=str(exc))

    def _track_note_effectiveness(
        self, session: Session, recommendation_outcome_id: uuid.UUID
    ) -> tuple[list[NoteEffectivenessEvaluation], str | None]:
        """Evaluate and persist this outcome's own note-effectiveness rows (Milestone 7 extension).

        NEVER raises -- mirrors ``_maybe_recalibrate_price_signal``'s own failure-isolation
        contract exactly (see this module's own docstring): this is additional, informational
        tracking layered on top of GAMEWEEK_REVIEW's own primary work, not a step that should
        ever be able to fail the review itself. Returns ``([], error_message)`` on any failure,
        ``(rows, None)`` otherwise -- an empty ``rows`` list is NOT itself a failure (it is the
        ordinary case whenever no active note was applied to this gameweek's recommended squad).
        """
        try:
            return evaluate_note_effectiveness_for_outcome(session, recommendation_outcome_id), None
        except Exception as exc:  # noqa: BLE001 - never propagates, see this method's own docstring
            session.rollback()
            logger.warning(
                "gameweek_review.note_effectiveness_tracking_failed",
                extra={"recommendation_outcome_id": str(recommendation_outcome_id), "error": str(exc)},
            )
            return [], str(exc)
