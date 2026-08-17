"""``POST /jobs/review-gameweek``, ``POST /jobs/backtest``, and ``/learning`` endpoints: trigger
GAMEWEEK_REVIEW/BACKTEST and read their own persisted
``learning_reports``/``learning_signals``/``failure_attributions``/``backtest_runs`` rows.

Milestone 7 Layer 4 (``/jobs/review-gameweek``/``/learning``) and Layer 6 (``/jobs/backtest``).
Both POST endpoints mirror ``/recommendations/generate``'s (and, before that, ``/optimize/chips``'s)
exact shape: ``execute_job`` -> 409-with-``error_code`` on non-success -> assembled response on
success. The GET endpoints are plain, paginated reads over already-persisted rows -- no job
execution involved.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.jobs.service import execute_job
from apps.api.dependencies import get_db
from apps.api.schemas.learning import (
    BacktestRequest,
    BacktestRunOut,
    ColdStartValidationRequest,
    ColdStartValidationRunOut,
    FailureAttributionOut,
    GameweekReviewRequest,
    LearningReportOut,
    LearningSignalOut,
)
from packages.database.models.backtest_run import BacktestRun
from packages.database.models.cold_start_validation_run import ColdStartValidationRun
from packages.database.models.failure_attribution import FailureAttribution
from packages.database.models.gameweek import Gameweek
from packages.database.models.learning_report import LearningReport
from packages.database.models.learning_signal import LearningSignal
from packages.database.models.model_version import ModelVersion
from packages.database.models.season import Season
from packages.domain.jobs import JobStatus, JobType, TriggerType
from packages.domain.learning import FailureCategory, FailureReasonCode, LearningSignalType

router_jobs = APIRouter(prefix="/jobs", tags=["learning"])
router_learning = APIRouter(prefix="/learning", tags=["learning"])

# List endpoints below are unbounded-filter, bounded-result reads -- a fixed cap, not full pagination
# controls, since this codebase's own query volume (at most one GAMEWEEK_REVIEW run per real
# gameweek) never approaches a size where offset/limit paging is actually needed yet.
_MAX_RESULTS = 200


def _error_response(job) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "error_code": job.error_code,
            "error_message": job.error_message,
            "job_id": str(job.id),
        },
    )


def _not_found_report_response(learning_report_id: uuid.UUID | None = None) -> JSONResponse:
    detail = (
        f"learning_reports row {learning_report_id} not found."
        if learning_report_id is not None
        else "No learning_reports row exists for the current season yet."
    )
    return JSONResponse(
        status_code=404, content={"error_code": "LEARNING_REPORT_NOT_FOUND", "detail": detail}
    )


def _assembled_report(db: Session, report: LearningReport) -> LearningReportOut:
    gameweek = db.get(Gameweek, report.gameweek_id)
    assert gameweek is not None  # gameweek_id is a NOT NULL FK to a real row
    return LearningReportOut(
        learning_report_id=report.id,
        season_id=report.season_id,
        gameweek_id=report.gameweek_id,
        target_fpl_event_id=gameweek.fpl_event_id,
        run_status=report.run_status.value,
        recommendation_outcome_id=report.recommendation_outcome_id,
        failure_attribution_count=report.failure_attribution_count,
        learning_signals_computed=report.learning_signals_computed_json,
        report_markdown=report.report_markdown,
        summary=report.summary_json,
        notable_findings=report.notable_findings_json,
        retraining_triggered=report.retraining_triggered,
        retraining_job_id=report.retraining_job_id,
        started_at=report.started_at,
        completed_at=report.completed_at,
    )


def _signal_out(row: LearningSignal) -> LearningSignalOut:
    return LearningSignalOut(
        learning_signal_id=row.id,
        season_id=row.season_id,
        as_of_gameweek_id=row.as_of_gameweek_id,
        signal_type=row.signal_type.value,
        window_size_gameweeks=row.window_size_gameweeks,
        value=row.value,
        sample_count=row.sample_count,
        threshold_breached=row.threshold_breached,
        threshold_config_snapshot=row.threshold_config_snapshot_json,
        computed_at=row.computed_at,
    )


def _failure_out(row: FailureAttribution) -> FailureAttributionOut:
    return FailureAttributionOut(
        failure_attribution_id=row.id,
        recommendation_outcome_id=row.recommendation_outcome_id,
        reason_code=row.reason_code.value,
        category=row.category.value,
        severity=row.severity,
        evidence=row.evidence_json,
        created_at=row.created_at,
    )


def _assembled_backtest_run(db: Session, row: BacktestRun) -> BacktestRunOut:
    model_version = db.get(ModelVersion, row.model_version_id)
    assert model_version is not None  # model_version_id is a NOT NULL FK to a real row
    return BacktestRunOut(
        backtest_run_id=row.id,
        model_version_id=row.model_version_id,
        model_family=model_version.model_family.value,
        feature_version=row.feature_version,
        run_status=row.run_status.value,
        usable_seasons=row.usable_seasons_json,
        fold_count=row.fold_count,
        degenerate_single_season=row.degenerate_single_season,
        fold_metrics=row.fold_metrics_json,
        aggregate_mae=row.aggregate_mae,
        aggregate_rmse=row.aggregate_rmse,
        aggregate_mean_bias=row.aggregate_mean_bias,
        aggregate_spearman=row.aggregate_spearman,
        aggregate_top_20_capture_rate=row.aggregate_top_20_capture_rate,
        n_observations=row.n_observations,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


@router_jobs.post("/review-gameweek", response_model=LearningReportOut)
def review_gameweek(
    body: GameweekReviewRequest, db: Session = Depends(get_db)
) -> LearningReportOut | JSONResponse:
    parameters: dict[str, Any] = {"target_fpl_event_id": body.target_fpl_event_id}
    if body.season_id is not None:
        parameters["season_id"] = str(body.season_id)

    job = execute_job(
        db,
        job_type=JobType.GAMEWEEK_REVIEW,
        parameters=parameters,
        requested_by=body.requested_by,
        trigger_type=TriggerType.API,
    )
    if job.status != JobStatus.SUCCEEDED:
        return _error_response(job)

    assert job.output_json is not None  # a SUCCEEDED job always has output_json
    learning_report_id = uuid.UUID(job.output_json["learning_report_id"])
    report = db.get(LearningReport, learning_report_id)
    assert report is not None  # a SUCCEEDED job always leaves its learning_reports row behind
    return _assembled_report(db, report)


def _assembled_cold_start_validation_run(row: ColdStartValidationRun) -> ColdStartValidationRunOut:
    return ColdStartValidationRunOut(
        cold_start_validation_run_id=row.id,
        run_status=row.run_status.value,
        early_season_gameweek_cutoff=row.early_season_gameweek_cutoff,
        component_evaluated=row.component_evaluated,
        usable_seasons=row.usable_seasons_json,
        seasons_evaluated=row.seasons_evaluated_json,
        n_observations=row.n_observations,
        half_life_sweep=row.half_life_sweep_json,
        default_half_life_seasons=row.default_half_life_seasons,
        default_half_life_mae=row.default_half_life_mae,
        best_half_life_seasons=row.best_half_life_seasons,
        best_half_life_label=row.best_half_life_label,
        best_half_life_mae=row.best_half_life_mae,
        difficulty_coefficient_sweep=row.difficulty_coefficient_sweep_json,
        default_difficulty_coefficient=row.default_difficulty_coefficient,
        default_difficulty_coefficient_mae=row.default_difficulty_coefficient_mae,
        best_difficulty_coefficient=row.best_difficulty_coefficient,
        best_difficulty_coefficient_mae=row.best_difficulty_coefficient_mae,
        blend_ratio_sweep=row.blend_ratio_sweep_json,
        default_blend_career_avg_weight=row.default_blend_career_avg_weight,
        default_blend_mae=row.default_blend_mae,
        best_blend_career_avg_weight=row.best_blend_career_avg_weight,
        best_blend_mae=row.best_blend_mae,
        blend_ratio_caveat=row.blend_ratio_caveat,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


@router_jobs.post("/backtest", response_model=BacktestRunOut)
def run_backtest(body: BacktestRequest, db: Session = Depends(get_db)) -> BacktestRunOut | JSONResponse:
    job = execute_job(
        db,
        job_type=JobType.BACKTEST,
        parameters={"model_version_id": str(body.model_version_id)},
        requested_by=body.requested_by,
        trigger_type=TriggerType.API,
    )
    if job.status != JobStatus.SUCCEEDED:
        return _error_response(job)

    assert job.output_json is not None  # a SUCCEEDED job always has output_json
    backtest_run_id = uuid.UUID(job.output_json["backtest_run_id"])
    row = db.get(BacktestRun, backtest_run_id)
    assert row is not None  # a SUCCEEDED job always leaves its backtest_runs row behind
    return _assembled_backtest_run(db, row)


@router_jobs.post("/cold-start-validation", response_model=ColdStartValidationRunOut)
def run_cold_start_validation(
    body: ColdStartValidationRequest, db: Session = Depends(get_db)
) -> ColdStartValidationRunOut | JSONResponse:
    parameters: dict[str, Any] = {}
    if body.half_life_candidates is not None:
        parameters["half_life_candidates"] = body.half_life_candidates
    if body.difficulty_coefficient_candidates is not None:
        parameters["difficulty_coefficient_candidates"] = body.difficulty_coefficient_candidates
    if body.blend_ratio_candidates is not None:
        parameters["blend_ratio_candidates"] = body.blend_ratio_candidates

    job = execute_job(
        db,
        job_type=JobType.COLD_START_PROJECTION_VALIDATION,
        parameters=parameters or None,
        requested_by=body.requested_by,
        trigger_type=TriggerType.API,
    )
    if job.status != JobStatus.SUCCEEDED:
        return _error_response(job)

    assert job.output_json is not None  # a SUCCEEDED job always has output_json
    run_id = uuid.UUID(job.output_json["cold_start_validation_run_id"])
    row = db.get(ColdStartValidationRun, run_id)
    assert row is not None  # a SUCCEEDED job always leaves its cold_start_validation_runs row behind
    return _assembled_cold_start_validation_run(row)


@router_learning.get("/latest", response_model=LearningReportOut)
def get_latest_learning_report(db: Session = Depends(get_db)) -> LearningReportOut | JSONResponse:
    report = (
        db.execute(
            select(LearningReport)
            .join(Season, Season.id == LearningReport.season_id)
            .where(Season.is_current.is_(True))
            .order_by(LearningReport.started_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    if report is None:
        return _not_found_report_response()
    return _assembled_report(db, report)


@router_learning.get("/reports/{learning_report_id}", response_model=LearningReportOut)
def get_learning_report(
    learning_report_id: uuid.UUID, db: Session = Depends(get_db)
) -> LearningReportOut | JSONResponse:
    report = db.get(LearningReport, learning_report_id)
    if report is None:
        return _not_found_report_response(learning_report_id)
    return _assembled_report(db, report)


@router_learning.get("/backtests/{backtest_run_id}", response_model=BacktestRunOut)
def get_backtest_run(
    backtest_run_id: uuid.UUID, db: Session = Depends(get_db)
) -> BacktestRunOut | JSONResponse:
    row = db.get(BacktestRun, backtest_run_id)
    if row is None:
        return JSONResponse(
            status_code=404,
            content={
                "error_code": "BACKTEST_RUN_NOT_FOUND",
                "detail": f"backtest_runs row {backtest_run_id} not found.",
            },
        )
    return _assembled_backtest_run(db, row)


@router_learning.get("/backtests", response_model=list[BacktestRunOut])
def list_backtest_runs(
    model_version_id: uuid.UUID | None = Query(None), db: Session = Depends(get_db)
) -> list[BacktestRunOut]:
    stmt = select(BacktestRun)
    if model_version_id is not None:
        stmt = stmt.where(BacktestRun.model_version_id == model_version_id)
    stmt = stmt.order_by(BacktestRun.started_at.desc()).limit(_MAX_RESULTS)
    rows = db.execute(stmt).scalars().all()
    return [_assembled_backtest_run(db, row) for row in rows]


@router_learning.get(
    "/cold-start-validations/{cold_start_validation_run_id}", response_model=ColdStartValidationRunOut
)
def get_cold_start_validation_run(
    cold_start_validation_run_id: uuid.UUID, db: Session = Depends(get_db)
) -> ColdStartValidationRunOut | JSONResponse:
    row = db.get(ColdStartValidationRun, cold_start_validation_run_id)
    if row is None:
        return JSONResponse(
            status_code=404,
            content={
                "error_code": "COLD_START_VALIDATION_RUN_NOT_FOUND",
                "detail": f"cold_start_validation_runs row {cold_start_validation_run_id} not found.",
            },
        )
    return _assembled_cold_start_validation_run(row)


@router_learning.get("/cold-start-validations", response_model=list[ColdStartValidationRunOut])
def list_cold_start_validation_runs(db: Session = Depends(get_db)) -> list[ColdStartValidationRunOut]:
    stmt = (
        select(ColdStartValidationRun).order_by(ColdStartValidationRun.started_at.desc()).limit(_MAX_RESULTS)
    )
    rows = db.execute(stmt).scalars().all()
    return [_assembled_cold_start_validation_run(row) for row in rows]


@router_learning.get("/signals", response_model=list[LearningSignalOut])
def list_learning_signals(
    signal_type: str | None = Query(None, description="Filter by LearningSignalType value"),
    db: Session = Depends(get_db),
) -> list[LearningSignalOut]:
    stmt = select(LearningSignal)
    if signal_type is not None:
        try:
            parsed_signal_type = LearningSignalType(signal_type)
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"Unrecognized signal_type '{signal_type}'."
            ) from None
        stmt = stmt.where(LearningSignal.signal_type == parsed_signal_type)
    stmt = stmt.order_by(LearningSignal.computed_at.desc()).limit(_MAX_RESULTS)
    rows = db.execute(stmt).scalars().all()
    return [_signal_out(row) for row in rows]


@router_learning.get("/failures", response_model=list[FailureAttributionOut])
def list_failure_attributions(
    reason_code: str | None = Query(None, description="Filter by FailureReasonCode value"),
    category: str | None = Query(None, description="Filter by FailureCategory value"),
    db: Session = Depends(get_db),
) -> list[FailureAttributionOut]:
    stmt = select(FailureAttribution)
    if reason_code is not None:
        try:
            parsed_reason_code = FailureReasonCode(reason_code)
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"Unrecognized reason_code '{reason_code}'."
            ) from None
        stmt = stmt.where(FailureAttribution.reason_code == parsed_reason_code)
    if category is not None:
        try:
            parsed_category = FailureCategory(category)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Unrecognized category '{category}'.") from None
        stmt = stmt.where(FailureAttribution.category == parsed_category)
    stmt = stmt.order_by(FailureAttribution.created_at.desc()).limit(_MAX_RESULTS)
    rows = db.execute(stmt).scalars().all()
    return [_failure_out(row) for row in rows]
