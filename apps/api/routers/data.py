"""POST /data/refresh -- an on-demand trigger for DATA_INGESTION from the UI.

DATA_INGESTION previously had no HTTP trigger at all (CLI/`make ingest` and
the scheduled dispatcher only) -- see ARCHITECTURE.md's job-type inventory.
That gap became a real, live problem: a manager checking their squad through
the UI had no way to force a refresh before deciding, and the data could sit
stale for weeks (confirmed live this session -- a real 19-day-stale ingestion
missed 23 real summer transfers/signings) with nothing to fix it short of
running a script by hand.

Deliberately synchronous, exactly like the other job-triggering endpoints in
this codebase (`/managers/sync`, `/leagues/sync`) -- `execute_job` already
blocks until the job finishes and this codebase has no job-id/polling
infrastructure for any endpoint, so this doesn't need one either. A full
ingestion run is fast (low seconds, confirmed live), well within a normal
request/response cycle.
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.jobs.service import execute_job
from apps.api.dependencies import get_db
from apps.api.schemas.data import DataRefreshOut
from packages.domain.jobs import JobStatus, JobType, TriggerType

router = APIRouter(prefix="/data", tags=["data"])


def _error_response(job) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "error_code": job.error_code,
            "error_message": job.error_message,
            "job_id": str(job.id),
        },
    )


@router.post("/refresh", response_model=DataRefreshOut)
def refresh_data(db: Session = Depends(get_db)) -> DataRefreshOut | JSONResponse:
    job = execute_job(
        db,
        job_type=JobType.DATA_INGESTION,
        parameters={},
        requested_by="web_ui",
        trigger_type=TriggerType.API,
    )
    if job.status != JobStatus.SUCCEEDED:
        return _error_response(job)

    assert job.output_json is not None  # a SUCCEEDED job always has output_json
    output = job.output_json
    normalization = output.get("normalization") or {}
    players = normalization.get("players") or {}

    return DataRefreshOut(
        ingestion_run_id=output["ingestion_run_id"],
        status=output["status"],
        season_label=output.get("season_label"),
        data_quality_score=output.get("data_quality_score"),
        optimization_permitted=bool(output.get("optimization_permitted", False)),
        players_inserted=int(players.get("inserted", 0)),
        players_updated=int(players.get("updated", 0)),
        issue_count=int(output.get("issue_count", 0)),
    )
