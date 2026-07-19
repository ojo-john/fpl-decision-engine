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
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.jobs.registry import register_handler
from app.jobs.runner import JobContext, JobResult, JobRunner
from app.learning.attribution import attribute_failures_for_outcome
from app.learning.outcome_ingestion import ingest_outcome_for_audit
from app.learning.report import (
    CounterfactualResultSummary,
    FailureAttributionSummary,
    LearningSignalSummary,
    OutcomeSummary,
    RecommendationSummary,
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
from packages.database.models.chip_optimization_run import ChipOptimizationRun
from packages.database.models.counterfactual_result import CounterfactualResult
from packages.database.models.gameweek import Gameweek
from packages.database.models.h2h_simulation_run import H2HSimulationRun
from packages.database.models.learning_report import LearningReport
from packages.database.models.optimization_run import OptimizationRun
from packages.database.models.recommendation_audit import RecommendationAudit
from packages.database.models.season import Season
from packages.database.models.transfer_optimization_run import TransferOptimizationRun
from packages.database.session import SessionLocal
from packages.domain.jobs import JobStatus, JobType
from packages.domain.learning import GameweekReviewRunStatus


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

                notable_findings = compose_notable_findings(failure_summaries, signal_summaries)
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
                }

                report_row.recommendation_outcome_id = outcome.id
                report_row.failure_attribution_count = len(attributions)
                report_row.learning_signals_computed_json = learning_signals_computed
                report_row.report_markdown = report_markdown
                report_row.summary_json = summary
                report_row.notable_findings_json = notable_findings
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
