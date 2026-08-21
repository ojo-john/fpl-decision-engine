#!/usr/bin/env python
"""Local background scheduler: runs `app.scheduling.tick.run_tick` on a loop.

Thin CLI/process wrapper, same "app package has the real logic, scripts/ has a thin shim" pattern
`scripts/run_job.py` already establishes for `app.jobs.service.execute_job`. All the real
scheduling logic (what runs, when, and why) lives in `app.scheduling.tick` -- see that module's own
docstring, including why it's also reused directly by `apps.api.routers.internal`'s HTTP-triggered
tick endpoint (for hosting targets with no persistent background-worker option).

Opt-in: this only runs if the `scheduler` service in `docker-compose.yml` is started (see that
file's own comment on how to omit it). It only works while the host machine/Docker stack is
actually running -- see `docs/operations.md` for why this is a deliberate, local-only substitute
for real hosting, not a replacement for one. `docs/deployment.md` covers the real hosted
alternative (the HTTP tick endpoint + an external cron trigger) for when this script itself isn't
an option (e.g. a free-tier host with no persistent background-worker service).
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

# Same sys.path fix scripts/run_job.py already uses -- see that file's own comment for why a
# plain script invocation needs this (pytest's pythonpath config doesn't apply here).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.scheduling.tick import (  # noqa: E402
    DATA_INGESTION_INTERVAL,
    LEAGUE_SYNC_INTERVAL,
    run_tick,
)
from packages.shared.logging import configure_logging  # noqa: E402

logger = logging.getLogger(__name__)

# How often the scheduler wakes up to re-check everything. Deliberately short relative to the
# interval-based jobs (they track their own "was it actually time yet" state via the real jobs
# table -- see app.scheduling.tick) -- this just needs to be frequent enough that GAMEWEEK_REVIEW
# fires reasonably promptly after a gameweek actually finishes, without hammering the database.
# 15 minutes is a reasonable balance: a gameweek finishing is a rare, clustered event (once a
# week), not something that needs second-level reaction time.
TICK_INTERVAL_SECONDS = 15 * 60


def main() -> None:
    configure_logging()
    logger.info(
        "scheduler.starting",
        extra={
            "tick_interval_seconds": TICK_INTERVAL_SECONDS,
            "data_ingestion_interval_hours": DATA_INGESTION_INTERVAL.total_seconds() / 3600,
            "league_sync_interval_hours": LEAGUE_SYNC_INTERVAL.total_seconds() / 3600,
        },
    )
    while True:
        run_tick()
        time.sleep(TICK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
