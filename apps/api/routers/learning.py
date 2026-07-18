"""``POST /jobs/review-gameweek`` and ``/learning`` endpoints: trigger GAMEWEEK_REVIEW and read its
own persisted ``learning_reports``/``learning_signals``/``failure_attributions`` rows.

Milestone 7 Layer 4. ``POST /jobs/review-gameweek`` mirrors ``/recommendations/generate``'s (and,
before that, ``/optimize/chips``'s) exact shape: ``execute_job`` -> 409-with-``error_code`` on
non-success -> assembled response on success. The 4 GET endpoints are plain, paginated reads over
already-persisted rows -- no job execution involved.
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
    FailureAttributionOut,
    GameweekReviewRequest,
    LearningReportOut,
    LearningSignalOut,
)
from packages.database.models.failure_attribution import FailureAttribution
from packages.database.models.gameweek import Gameweek
from packages.database.models.learning_report import LearningReport
from packages.database.models.learning_signal import LearningSignal
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
