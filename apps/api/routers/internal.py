"""POST /internal/scheduler-tick -- real, stateless trigger for one scheduler tick.

Exists so an external cron service (e.g. cron-job.org) can drive the exact same
LEAGUE_SYNC/GAMEWEEK_REVIEW/DATA_INGESTION scheduling logic `scripts/scheduler.py`'s own local
loop already runs, for deployments where a persistent background process isn't available (e.g.
Render's free web-service tier, which only runs in response to real HTTP traffic -- no free
"background worker" service that stays alive independently). Both this endpoint and the local
loop call the exact same `app.scheduling.tick.run_tick` function -- no duplicated logic, and both
are correct from a cold start, since `run_tick` derives all of its own "when did X last run" state
fresh from the real `jobs` table every single call (see that module's own docstring for why this
needed no separate state-tracking mechanism).

Authenticated exactly like every other endpoint (see `apps.api.main`'s own
`dependencies=[protected]` wiring when this router is included) -- this is a real,
internet-reachable endpoint once deployed, unlike everything else in this project that ran on
localhost-only throughout development, so it gets no special exemption from the shared-token auth.
Recommended real polling interval once deployed: every 15 minutes, matching
`scripts/scheduler.py`'s own local tick interval -- see `docs/deployment.md` for the concrete
cron-job.org setup. Safe to call more often (each call is a real, complete, independent tick that
only actually triggers a job when something is genuinely due), but there's no benefit to it.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.scheduling.tick import run_tick

router = APIRouter(prefix="/internal", tags=["internal"])


@router.post("/scheduler-tick")
def trigger_scheduler_tick() -> JSONResponse:
    """Run exactly one real scheduler tick, synchronously.

    Never raises -- `run_tick` already isolates every real failure internally (see its own
    docstring), so this always returns 200 once the tick completes, regardless of what happened
    inside it; check server logs for the real per-job outcome (mirrors `run_tick`'s own
    established resilience discipline: a scheduling boundary must never itself fail the caller).
    """
    run_tick()
    return JSONResponse(status_code=200, content={"status": "tick_completed"})
