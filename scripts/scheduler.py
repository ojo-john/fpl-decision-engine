#!/usr/bin/env python
"""Local background scheduler: keeps the real database current with zero human interaction.

Real gap this closes: `.github/workflows/scheduled-dispatcher.yml`/`ingest.yml` each spin up
their OWN throwaway, empty Postgres service container inside the GitHub Actions runner -- they
have NEVER touched the real local database this repo's own `docker-compose.yml` `postgres`
service serves, and structurally CANNOT (GitHub's runners have no network path to a user's own
machine). Every real job run against the real database, all project long, happened because a
human manually ran `scripts/run_job.py` or hit an API endpoint. `league-watch.yml`/
`scout-watch.yml` are the only genuinely-automated things that exist, and they deliberately avoid
this problem by never touching the database at all (polling FPL's public API directly and writing
to a committed JSON file; or calling an LLM and opening a GitHub issue).

This script is the real fix, scoped to what's actually safe to automate unattended:

- `LEAGUE_SYNC` -- real rival picks only change post-deadline; safe to poll periodically.
- `GAMEWEEK_REVIEW` -- triggered once, automatically, the moment a gameweek is both `finished`
  and not already reviewed (checked via a real `learning_reports` row, not guessed) -- never
  re-run for a gameweek that already has one. `GAMEWEEK_REVIEW` itself hard-refuses
  (`NO_AUTHORITATIVE_RECOMMENDATION`) without an existing `is_authoritative=True`
  `recommendation_audits` row for that gameweek, and nothing previously created one automatically
  -- this scheduler now checks for that first and calls `RECOMMENDATION_GENERATION` (with an
  explicit `target_fpl_event_id`, so it needs no manager-squad-snapshot/current-season resolution
  of its own) to produce one before attempting the review. `RECOMMENDATION_GENERATION` is a pure,
  best-effort correlator, not a hard-precondition gate -- it degrades to a real but LOW-confidence
  audit (see its own module docstring's `compute_confidence`) when the 4 optimizer/simulation
  components it looks for don't have a run for this gameweek (e.g. pre-deadline, no manager squad
  ever synced for it -- an expected state this whole project has hit repeatedly), rather than
  failing outright. If it still fails for some other real reason, `GAMEWEEK_REVIEW` will simply
  fail its own `NO_AUTHORITATIVE_RECOMMENDATION` check right after (logged, not fatal to the
  loop) and both are retried next tick, since the gameweek is correctly still "not reviewed yet".
- `DATA_INGESTION` -- keeps live player/price/status data current, mirroring the cadence
  `scheduled-dispatcher.yml`'s own CI self-test already reasoned about (every 4 hours) for the
  real thing this time.

Deliberately does NOT trigger `MODEL_TRAINING`/any promotion-adjacent job directly --
`GAMEWEEK_REVIEW` already has its own internal trigger for that on a sustained MAE breach; a
second, redundant path here would just double-fire it.

Design choice: calls `app.jobs.service.execute_job` directly (same pattern as `scripts/run_job.py`),
not the HTTP API. Both converge on the exact same function eventually (the API routers are thin
wrappers around it), so calling it directly avoids an unnecessary network hop, avoids needing to
manage/rotate an HTTP bearer token inside this container, and keeps this script trivially reusable
outside Docker (e.g. a plain `cron` entry on a host that isn't using compose at all).

Opt-in: this only runs if the `scheduler` service in `docker-compose.yml` is started (see that
file's own comment on how to omit it). It only works while the host machine/Docker stack is
actually running -- see `docs/operations.md` for why this is a deliberate, local-only substitute
for real hosting, not a replacement for one.
"""

from __future__ import annotations

import logging
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Same sys.path fix scripts/run_job.py already uses -- see that file's own comment for why a
# plain script invocation needs this (pytest's pythonpath config doesn't apply here).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.jobs.service import execute_job  # noqa: E402
from packages.database.models.gameweek import Gameweek  # noqa: E402
from packages.database.models.learning_report import LearningReport  # noqa: E402
from packages.database.models.recommendation_audit import RecommendationAudit  # noqa: E402
from packages.database.models.season import Season  # noqa: E402
from packages.database.session import SessionLocal  # noqa: E402
from packages.domain.jobs import JobStatus, JobType, TriggerType  # noqa: E402
from packages.shared.config import get_settings  # noqa: E402
from packages.shared.logging import configure_logging  # noqa: E402

logger = logging.getLogger(__name__)

# How often the scheduler wakes up to re-check everything. Deliberately short relative to the
# interval-based jobs below (they track their own "was it actually time yet" state) -- this just
# needs to be frequent enough that GAMEWEEK_REVIEW fires reasonably promptly after a gameweek
# actually finishes, without hammering the database. 15 minutes is a reasonable balance: a
# gameweek finishing is a rare, clustered event (once a week), not something that needs
# second-level reaction time.
TICK_INTERVAL_SECONDS = 15 * 60

# Mirrors scheduled-dispatcher.yml's own reasoned cadence for its CI self-test of the same job
# (`cron: "0 */4 * * *"`) -- applied here to the real thing. FPL's own live data (prices, status,
# ownership) doesn't change meaningfully faster than this in practice.
DATA_INGESTION_INTERVAL = timedelta(hours=4)

# Real rival picks are only ever knowable post-deadline and don't change intra-week once known;
# a few times a day is more than enough to catch a rival's picks becoming available shortly after
# a deadline passes, without wastefully re-hitting FPL's public league API every tick.
LEAGUE_SYNC_INTERVAL = timedelta(hours=6)


@dataclass(frozen=True)
class GameweekState:
    """Plain, DB-free snapshot of one gameweek -- lets the decision functions below be pure."""

    fpl_event_id: int
    finished: bool


def gameweeks_needing_review(
    gameweeks: list[GameweekState], already_reviewed_event_ids: set[int]
) -> list[int]:
    """Real fpl_event_ids that are finished but have no learning_reports row yet.

    Pure function, no DB/HTTP -- the caller resolves both inputs from real state first. A
    gameweek that is finished AND already reviewed is correctly excluded (GAMEWEEK_REVIEW's own
    `learning_reports` table is insert-only per run -- see that handler's module docstring -- so
    calling it again would silently create a second, redundant report every single tick forever
    if this check didn't exist).
    """
    return sorted(
        gw.fpl_event_id
        for gw in gameweeks
        if gw.finished and gw.fpl_event_id not in already_reviewed_event_ids
    )


def is_interval_due(last_run_at: datetime | None, now: datetime, interval: timedelta) -> bool:
    """True if `interval` has elapsed since `last_run_at` (or nothing has ever run yet)."""
    if last_run_at is None:
        return True
    return now - last_run_at >= interval


def event_ids_missing_authoritative_recommendation(
    due_event_ids: list[int], has_authoritative_ids: set[int]
) -> list[int]:
    """Real fpl_event_ids that need `RECOMMENDATION_GENERATION` before `GAMEWEEK_REVIEW` can run.

    Pure function, no DB/HTTP -- mirrors `gameweeks_needing_review`'s own shape exactly. Scoped to
    `due_event_ids` (already known to be finished-and-unreviewed) rather than every gameweek, since
    an event that's already reviewed or not yet finished has no reason to need one generated here.
    """
    return sorted(event_id for event_id in due_event_ids if event_id not in has_authoritative_ids)


def _fetch_gameweek_review_state(session: Session) -> tuple[uuid.UUID | None, list[int]]:
    """`(season_id, real fpl_event_ids needing GAMEWEEK_REVIEW right now)`, resolved from the real
    database. `season_id` is also returned (not just the event ids) since the caller needs it
    again for `_fetch_authoritative_recommendation_event_ids` -- `None` whenever no current season
    exists yet (mirrors the empty-list short-circuit this function already had)."""
    season_id = session.execute(select(Season.id).where(Season.is_current.is_(True))).scalar_one_or_none()
    if season_id is None:
        return None, []

    gameweeks = [
        GameweekState(fpl_event_id=fpl_event_id, finished=finished)
        for fpl_event_id, finished in session.execute(
            select(Gameweek.fpl_event_id, Gameweek.finished).where(Gameweek.season_id == season_id)
        )
    ]
    already_reviewed = {
        fpl_event_id
        for (fpl_event_id,) in session.execute(
            select(Gameweek.fpl_event_id)
            .join(LearningReport, LearningReport.gameweek_id == Gameweek.id)
            .where(Gameweek.season_id == season_id)
        )
    }
    return season_id, gameweeks_needing_review(gameweeks, already_reviewed)


def _fetch_authoritative_recommendation_event_ids(
    session: Session, season_id: uuid.UUID, event_ids: list[int]
) -> set[int]:
    """Real fpl_event_ids (scoped to `event_ids`) that already have an authoritative recommendation.

    `RecommendationAudit.target_fpl_event_id` is a direct column (see that model) -- no join to
    `Gameweek` needed, unlike `_fetch_gameweek_review_state` above.
    """
    if not event_ids:
        return set()
    return {
        fpl_event_id
        for (fpl_event_id,) in session.execute(
            select(RecommendationAudit.target_fpl_event_id).where(
                RecommendationAudit.season_id == season_id,
                RecommendationAudit.target_fpl_event_id.in_(event_ids),
                RecommendationAudit.is_authoritative.is_(True),
            )
        )
    }


def _run_job_safely(job_type: JobType, parameters: dict | None, requested_by: str) -> None:
    """Run one job against the real database, isolated so a failure never kills the scheduler loop.

    Mirrors GameweekReviewJobRunner's own established failure-isolation precedent
    (`_maybe_recalibrate_price_signal`/the retraining-trigger step): catch broadly here
    specifically because this is a top-level scheduling boundary where ANY failure (a network
    blip talking to the real FPL API, a transient DB issue, a genuine bug in the job itself) must
    never stop future ticks from running other jobs. Each `execute_job` call gets its own fresh
    session so one job's failure can't leave a poisoned session for the next.
    """
    session = SessionLocal()
    try:
        job = execute_job(
            session,
            job_type=job_type,
            parameters=parameters,
            requested_by=requested_by,
            # LOCAL, not GITHUB_SCHEDULE -- this genuinely runs on the user's own machine, not
            # via GitHub Actions (the entire reason this script exists: GitHub Actions cannot
            # reach this database). Using GITHUB_SCHEDULE here would misleadingly imply the
            # opposite of what this module's own docstring establishes. `requested_by="scheduler"`
            # is what actually distinguishes this from a human's own manual `scripts/run_job.py`
            # invocation in the `jobs` table's audit trail.
            trigger_type=TriggerType.LOCAL,
        )
        level = logging.INFO if job.status == JobStatus.SUCCEEDED else logging.WARNING
        logger.log(
            level,
            "scheduler.job_completed",
            extra={
                "job_type": job_type.value,
                "job_id": str(job.id),
                "status": job.status.value,
                "error_code": job.error_code,
            },
        )
    except Exception as exc:  # noqa: BLE001 - top-level scheduling boundary, must never propagate
        logger.warning(
            "scheduler.job_failed",
            extra={"job_type": job_type.value, "error": str(exc)},
        )
    finally:
        session.close()


def run_tick(state: dict[str, datetime | None]) -> None:
    """One real scheduling pass: check what's due, run it, mutate `state` in place with new timestamps."""
    now = datetime.now(UTC)

    session = SessionLocal()
    try:
        season_id, due_event_ids = _fetch_gameweek_review_state(session)
        has_authoritative_recommendation_event_ids = (
            _fetch_authoritative_recommendation_event_ids(session, season_id, due_event_ids)
            if season_id is not None
            else set()
        )
    except Exception as exc:  # noqa: BLE001 - a failed check must not stop other checks this tick
        logger.warning("scheduler.gameweek_review_check_failed", extra={"error": str(exc)})
        due_event_ids = []
        has_authoritative_recommendation_event_ids = set()
    finally:
        session.close()

    needs_recommendation = event_ids_missing_authoritative_recommendation(
        due_event_ids, has_authoritative_recommendation_event_ids
    )
    for fpl_event_id in needs_recommendation:
        logger.info(
            "scheduler.triggering_recommendation_generation", extra={"target_fpl_event_id": fpl_event_id}
        )
        _run_job_safely(
            JobType.RECOMMENDATION_GENERATION,
            {"target_fpl_event_id": fpl_event_id},
            requested_by="scheduler",
        )

    for fpl_event_id in due_event_ids:
        logger.info("scheduler.triggering_gameweek_review", extra={"target_fpl_event_id": fpl_event_id})
        _run_job_safely(
            JobType.GAMEWEEK_REVIEW,
            {"target_fpl_event_id": fpl_event_id},
            requested_by="scheduler",
        )

    if is_interval_due(state.get("league_sync"), now, LEAGUE_SYNC_INTERVAL):
        fpl_league_id = get_settings().fpl_h2h_league_id
        if fpl_league_id:
            logger.info("scheduler.triggering_league_sync", extra={"fpl_league_id": fpl_league_id})
            _run_job_safely(
                JobType.LEAGUE_SYNC, {"fpl_league_id": int(fpl_league_id)}, requested_by="scheduler"
            )
        else:
            logger.info("scheduler.league_sync_skipped_no_league_configured")
        state["league_sync"] = now

    if is_interval_due(state.get("data_ingestion"), now, DATA_INGESTION_INTERVAL):
        logger.info("scheduler.triggering_data_ingestion")
        _run_job_safely(JobType.DATA_INGESTION, None, requested_by="scheduler")
        state["data_ingestion"] = now


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
    state: dict[str, datetime | None] = {"league_sync": None, "data_ingestion": None}
    while True:
        run_tick(state)
        time.sleep(TICK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
