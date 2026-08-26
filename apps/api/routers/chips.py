"""POST /optimize/chips endpoint, plus GET-by-id and GET list (history).

Uses the request's own DB session for the outer ``Job`` bookkeeping via
``execute_job`` — the job handler internally opens its own session for
``chip_optimization_runs``, exactly like ``apps.api.routers.h2h``/
``apps.api.routers.optimization`` already do; an established pattern, not a
new one.
"""

import uuid

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.chips.windows import ChipWindow, resolve_chip_windows
from app.jobs.service import execute_job
from apps.api.dependencies import get_db
from apps.api.schemas.chips import (
    ChipOptimizationRunOut,
    ChipOptimizationRunSummaryOut,
    ChipRecommendationOut,
    ChipScheduleAssignmentOut,
    ChipScheduleOptimizationRequest,
    ChipScheduleOptimizationRunOut,
    ChipScheduleOptimizationRunSummaryOut,
    ChipSimulationRequest,
    ChipWindowCandidateOut,
    ChipWindowOut,
)
from packages.database.models.chip_optimization_run import ChipOptimizationRun
from packages.database.models.chip_recommendation import ChipRecommendation
from packages.database.models.chip_schedule_assignment import ChipScheduleAssignment
from packages.database.models.chip_schedule_optimization_run import ChipScheduleOptimizationRun
from packages.database.models.chip_window_candidate import ChipWindowCandidate
from packages.database.models.gameweek import Gameweek
from packages.database.models.rules_config import RulesConfig
from packages.database.models.season import Season
from packages.domain.jobs import JobStatus, JobType, TriggerType

# Same "capped read, not full pagination" convention as apps.api.routers.optimization/managers.
_MAX_RESULTS = 200

router = APIRouter(prefix="/optimize", tags=["chips"])


def _error_response(job) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "error_code": job.error_code,
            "error_message": job.error_message,
            "job_id": str(job.id),
        },
    )


def _not_found_response(chip_optimization_run_id: uuid.UUID) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={
            "error_code": "CHIP_OPTIMIZATION_RUN_NOT_FOUND",
            "detail": f"chip_optimization_runs row {chip_optimization_run_id} not found.",
        },
    )


def _schedule_not_found_response(chip_schedule_optimization_run_id: uuid.UUID) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={
            "error_code": "CHIP_SCHEDULE_OPTIMIZATION_RUN_NOT_FOUND",
            "detail": f"chip_schedule_optimization_runs row {chip_schedule_optimization_run_id} not found.",
        },
    )


def _candidate_out(row: ChipWindowCandidate) -> ChipWindowCandidateOut:
    return ChipWindowCandidateOut(
        candidate_fpl_event_id=row.candidate_fpl_event_id,
        gain_mean=row.gain_mean,
        gain_floor=row.gain_floor,
        gain_ceiling=row.gain_ceiling,
        is_blank_gameweek=row.is_blank_gameweek,
        is_double_gameweek=row.is_double_gameweek,
        rank_within_chip_type=row.rank_within_chip_type,
        h2h_win_probability_with_chip=row.h2h_win_probability_with_chip,
        h2h_win_probability_without_chip=row.h2h_win_probability_without_chip,
    )


def _load_recommendations(db: Session, chip_optimization_run_id: uuid.UUID) -> list[ChipRecommendationOut]:
    recommendation_rows = (
        db.execute(
            select(ChipRecommendation)
            .where(ChipRecommendation.chip_optimization_run_id == chip_optimization_run_id)
            .order_by(ChipRecommendation.created_at)
        )
        .scalars()
        .all()
    )
    candidate_rows = (
        db.execute(
            select(ChipWindowCandidate)
            .where(ChipWindowCandidate.chip_optimization_run_id == chip_optimization_run_id)
            .order_by(ChipWindowCandidate.rank_within_chip_type)
        )
        .scalars()
        .all()
    )
    candidates_by_id = {row.id: row for row in candidate_rows}
    candidates_by_chip_type: dict = {}
    for row in candidate_rows:
        candidates_by_chip_type.setdefault(row.chip_type, []).append(row)

    recommendations: list[ChipRecommendationOut] = []
    for recommendation_row in recommendation_rows:
        recommended_candidate_row = (
            candidates_by_id.get(recommendation_row.recommended_candidate_id)
            if recommendation_row.recommended_candidate_id is not None
            else None
        )
        recommendations.append(
            ChipRecommendationOut(
                chip_type=recommendation_row.chip_type.value,
                verdict=recommendation_row.verdict.value,
                rationale=recommendation_row.rationale,
                recommended_candidate=(
                    _candidate_out(recommended_candidate_row)
                    if recommended_candidate_row is not None
                    else None
                ),
                candidates=[
                    _candidate_out(row)
                    for row in candidates_by_chip_type.get(recommendation_row.chip_type, [])
                ],
            )
        )
    return recommendations


def _load_run_response(db: Session, chip_optimization_run_id: uuid.UUID) -> ChipOptimizationRunOut:
    run = db.get(ChipOptimizationRun, chip_optimization_run_id)
    assert run is not None  # a SUCCEEDED job always leaves its chip_optimization_runs row behind
    return ChipOptimizationRunOut(
        chip_optimization_run_id=run.id,
        manager_squad_snapshot_id=run.manager_squad_snapshot_id,
        run_status=run.run_status.value,
        target_fpl_event_id=run.target_fpl_event_id,
        recommendations=_load_recommendations(db, chip_optimization_run_id),
        started_at=run.started_at,
        completed_at=run.completed_at,
    )


@router.post("/chips", response_model=ChipOptimizationRunOut)
def simulate_chip_optimization(
    body: ChipSimulationRequest, db: Session = Depends(get_db)
) -> ChipOptimizationRunOut | JSONResponse:
    parameters: dict[str, str | list[str]] = {
        "manager_squad_snapshot_id": str(body.manager_squad_snapshot_id),
    }
    if body.chip_types is not None:
        parameters["chip_types"] = body.chip_types

    job = execute_job(
        db,
        job_type=JobType.CHIP_SIMULATION,
        parameters=parameters,
        requested_by=body.requested_by,
        trigger_type=TriggerType.API,
    )
    if job.status != JobStatus.SUCCEEDED:
        return _error_response(job)

    assert job.output_json is not None  # a SUCCEEDED job always has output_json
    chip_optimization_run_id = uuid.UUID(job.output_json["chip_optimization_run_id"])
    return _load_run_response(db, chip_optimization_run_id)


@router.get("/chips", response_model=list[ChipOptimizationRunSummaryOut])
def list_chip_optimization_runs(
    manager_squad_snapshot_id: uuid.UUID | None = Query(None),
    db: Session = Depends(get_db),
) -> list[ChipOptimizationRunSummaryOut]:
    """Most-recent-first history of chip-optimization runs (Milestone 8 item 1)."""
    stmt = select(ChipOptimizationRun)
    if manager_squad_snapshot_id is not None:
        stmt = stmt.where(ChipOptimizationRun.manager_squad_snapshot_id == manager_squad_snapshot_id)
    stmt = stmt.order_by(ChipOptimizationRun.started_at.desc()).limit(_MAX_RESULTS)

    rows = db.execute(stmt).scalars().all()
    return [
        ChipOptimizationRunSummaryOut(
            chip_optimization_run_id=row.id,
            manager_squad_snapshot_id=row.manager_squad_snapshot_id,
            run_status=row.run_status.value,
            target_fpl_event_id=row.target_fpl_event_id,
            started_at=row.started_at,
            completed_at=row.completed_at,
        )
        for row in rows
    ]


def _window_status(window: ChipWindow, current_event_id: int | None) -> str:
    if current_event_id is None:
        return "upcoming"
    if current_event_id > window.stop_event:
        return "past"
    if window.start_event <= current_event_id <= window.stop_event:
        return "open"
    return "upcoming"


@router.get("/chips/windows", response_model=list[ChipWindowOut])
def get_chip_windows(db: Session = Depends(get_db)) -> list[ChipWindowOut] | JSONResponse:
    """Read-only chip-scheduling windows for the current season -- genuinely available
    pre-season (FPL publishes these before a ball is kicked), unlike full ``CHIP_SIMULATION``
    (needs a real squad, passes through ``check_optimization_gate``). MUST be registered before
    ``GET /chips/{chip_optimization_run_id}`` below -- Starlette matches routes in registration
    order and both are the same one-segment shape (see this router's own inclusion-order
    comment in ``apps/api/main.py`` for the established precedent this mirrors).

    Sourced from the current season's ``rules_config.raw_chips_json`` -- already-ingested,
    versioned data (populated by ``DATA_INGESTION``'s own ``RULES_SYNC`` step) -- never a fresh
    live FPL API call at request time.
    """
    season_id = db.execute(select(Season.id).where(Season.is_current.is_(True))).scalar_one_or_none()
    if season_id is None:
        return JSONResponse(
            status_code=404, content={"error_code": "NO_CURRENT_SEASON", "detail": "No current season found."}
        )

    rules_config = db.execute(
        select(RulesConfig).where(RulesConfig.season_id == season_id, RulesConfig.is_current.is_(True))
    ).scalar_one_or_none()
    if rules_config is None:
        return JSONResponse(
            status_code=404,
            content={
                "error_code": "NO_RULES_CONFIG",
                "detail": "No rules config snapshot exists yet -- run DATA_INGESTION first.",
            },
        )

    windows = resolve_chip_windows(rules_config.raw_chips_json)

    event_ids = {w.start_event for w in windows} | {w.stop_event for w in windows}
    deadline_by_event = (
        {
            row.fpl_event_id: row.deadline_time
            for row in db.execute(
                select(Gameweek).where(Gameweek.season_id == season_id, Gameweek.fpl_event_id.in_(event_ids))
            ).scalars()
        }
        if event_ids
        else {}
    )
    current_event_id = db.execute(
        select(Gameweek.fpl_event_id).where(Gameweek.season_id == season_id, Gameweek.is_current.is_(True))
    ).scalar_one_or_none()

    return [
        ChipWindowOut(
            chip_type=w.chip_type.value,
            window_number=w.window_number,
            start_event=w.start_event,
            stop_event=w.stop_event,
            start_deadline=deadline_by_event.get(w.start_event),
            stop_deadline=deadline_by_event.get(w.stop_event),
            status=_window_status(w, current_event_id),
        )
        for w in windows
    ]


def _load_schedule_run_response(
    db: Session, chip_schedule_optimization_run_id: uuid.UUID
) -> ChipScheduleOptimizationRunOut:
    run = db.get(ChipScheduleOptimizationRun, chip_schedule_optimization_run_id)
    assert run is not None  # a SUCCEEDED job always leaves its chip_schedule_optimization_runs row behind
    assignment_rows = (
        db.execute(
            select(ChipScheduleAssignment)
            .where(
                ChipScheduleAssignment.chip_schedule_optimization_run_id == chip_schedule_optimization_run_id
            )
            .order_by(ChipScheduleAssignment.chip_type, ChipScheduleAssignment.window_number)
        )
        .scalars()
        .all()
    )
    return ChipScheduleOptimizationRunOut(
        chip_schedule_optimization_run_id=run.id,
        manager_squad_snapshot_id=run.manager_squad_snapshot_id,
        source_chip_optimization_run_id=run.source_chip_optimization_run_id,
        run_status=run.run_status.value,
        total_expected_gain=run.total_expected_gain,
        assignments=[
            ChipScheduleAssignmentOut(
                chip_type=row.chip_type.value,
                window_number=row.window_number,
                assigned_fpl_event_id=row.assigned_fpl_event_id,
                gain_mean=row.gain_mean,
            )
            for row in assignment_rows
        ],
        started_at=run.started_at,
        completed_at=run.completed_at,
    )


@router.post("/chips/schedule", response_model=ChipScheduleOptimizationRunOut)
def optimize_chip_schedule(
    body: ChipScheduleOptimizationRequest, db: Session = Depends(get_db)
) -> ChipScheduleOptimizationRunOut | JSONResponse:
    """The joint chip scheduler -- see app.jobs.handlers.chip_schedule_optimization's own module
    docstring. A fresh, full-season, path-dependent evaluation directly from a
    manager_squad_snapshot_id (mirrors POST /chips's own input shape). MUST be registered before
    GET /chips/{chip_optimization_run_id} below -- same one-segment-path route-ordering reasoning
    as GET /chips/windows above (Starlette matches routes in registration order; "schedule" would
    otherwise be swallowed as a chip_optimization_run_id path parameter)."""
    parameters: dict[str, str] = {"manager_squad_snapshot_id": str(body.manager_squad_snapshot_id)}

    job = execute_job(
        db,
        job_type=JobType.CHIP_SCHEDULE_OPTIMIZATION,
        parameters=parameters,
        requested_by=body.requested_by,
        trigger_type=TriggerType.API,
    )
    if job.status != JobStatus.SUCCEEDED:
        return _error_response(job)

    assert job.output_json is not None  # a SUCCEEDED job always has output_json
    chip_schedule_optimization_run_id = uuid.UUID(job.output_json["chip_schedule_optimization_run_id"])
    return _load_schedule_run_response(db, chip_schedule_optimization_run_id)


@router.get("/chips/schedule", response_model=list[ChipScheduleOptimizationRunSummaryOut])
def list_chip_schedule_optimization_runs(
    manager_squad_snapshot_id: uuid.UUID | None = Query(None),
    db: Session = Depends(get_db),
) -> list[ChipScheduleOptimizationRunSummaryOut]:
    """Most-recent-first history, mirrors GET /chips's own identical shape/reasoning."""
    stmt = select(ChipScheduleOptimizationRun)
    if manager_squad_snapshot_id is not None:
        stmt = stmt.where(ChipScheduleOptimizationRun.manager_squad_snapshot_id == manager_squad_snapshot_id)
    stmt = stmt.order_by(ChipScheduleOptimizationRun.started_at.desc()).limit(_MAX_RESULTS)

    rows = db.execute(stmt).scalars().all()
    return [
        ChipScheduleOptimizationRunSummaryOut(
            chip_schedule_optimization_run_id=row.id,
            source_chip_optimization_run_id=row.source_chip_optimization_run_id,
            manager_squad_snapshot_id=row.manager_squad_snapshot_id,
            run_status=row.run_status.value,
            total_expected_gain=row.total_expected_gain,
            started_at=row.started_at,
            completed_at=row.completed_at,
        )
        for row in rows
    ]


@router.get(
    "/chips/schedule/{chip_schedule_optimization_run_id}", response_model=ChipScheduleOptimizationRunOut
)
def get_chip_schedule_optimization_run(
    chip_schedule_optimization_run_id: uuid.UUID, db: Session = Depends(get_db)
) -> ChipScheduleOptimizationRunOut | JSONResponse:
    if db.get(ChipScheduleOptimizationRun, chip_schedule_optimization_run_id) is None:
        return _schedule_not_found_response(chip_schedule_optimization_run_id)
    return _load_schedule_run_response(db, chip_schedule_optimization_run_id)


@router.get("/chips/{chip_optimization_run_id}", response_model=ChipOptimizationRunOut)
def get_chip_optimization_run(
    chip_optimization_run_id: uuid.UUID, db: Session = Depends(get_db)
) -> ChipOptimizationRunOut | JSONResponse:
    """Fetch an already-completed chip-optimization run by id (Milestone 8 Layer 1)."""
    if db.get(ChipOptimizationRun, chip_optimization_run_id) is None:
        return _not_found_response(chip_optimization_run_id)
    return _load_run_response(db, chip_optimization_run_id)
