"""Real scheduling logic: keeps the real database current with zero human interaction.

Real gap this closes: ``.github/workflows/scheduled-dispatcher.yml``/``ingest.yml`` each spin up
their OWN throwaway, empty Postgres service container inside the GitHub Actions runner -- they
have NEVER touched the real database this repo's own ``docker-compose.yml`` ``postgres`` service
(local) or a real deployed Postgres (once hosted) serves, and structurally CANNOT (GitHub's runners
have no network path to either). Every real job run against a real database, all project long,
happened because a human manually ran ``scripts/run_job.py`` or hit an API endpoint.
``league-watch.yml``/``scout-watch.yml`` are the only genuinely-automated things that exist, and
they deliberately avoid this problem by never touching the database at all (polling FPL's public
API directly and writing to a committed JSON file; or calling an LLM and opening a GitHub issue).

``run_tick`` (the one real entrypoint here) is the real fix, scoped to what's actually safe to
automate unattended:

- ``LEAGUE_SYNC`` -- real rival picks only change post-deadline; safe to poll periodically.
- ``GAMEWEEK_REVIEW`` -- triggered once, automatically, the moment a gameweek is both ``finished``
  and not already reviewed (checked via a real ``learning_reports`` row, not guessed) -- never
  re-run for a gameweek that already has one. ``GAMEWEEK_REVIEW`` itself hard-refuses
  (``NO_AUTHORITATIVE_RECOMMENDATION``) without an existing ``is_authoritative=True``
  ``recommendation_audits`` row for that gameweek, and nothing previously created one automatically
  -- this checks for that first and calls ``RECOMMENDATION_GENERATION`` (with an explicit
  ``target_fpl_event_id``, so it needs no manager-squad-snapshot/current-season resolution of its
  own) to produce one before attempting the review. ``RECOMMENDATION_GENERATION`` is a pure,
  best-effort correlator, not a hard-precondition gate -- it degrades to a real but LOW-confidence
  audit (see its own module docstring's ``compute_confidence``) when the 4 optimizer/simulation
  components it looks for don't have a run for this gameweek (e.g. pre-deadline, no manager squad
  ever synced for it -- an expected state this whole project has hit repeatedly), rather than
  failing outright. If it still fails for some other real reason, ``GAMEWEEK_REVIEW`` will simply
  fail its own ``NO_AUTHORITATIVE_RECOMMENDATION`` check right after (logged, not fatal) and both
  are retried next tick, since the gameweek is correctly still "not reviewed yet".
- ``DATA_INGESTION`` -- keeps live player/price/status data current, mirroring the cadence
  ``scheduled-dispatcher.yml``'s own CI self-test already reasoned about (every 4 hours) for the
  real thing this time.

Deliberately does NOT trigger ``MODEL_TRAINING``/any promotion-adjacent job directly --
``GAMEWEEK_REVIEW`` already has its own internal trigger for that on a sustained MAE breach; a
second, redundant path here would just double-fire it.

Design choice: calls ``app.jobs.service.execute_job`` directly (same pattern as
``scripts/run_job.py``), not the HTTP API, even from ``apps.api.routers.internal``'s own HTTP
endpoint wrapper around this. Both converge on the exact same function eventually (API routers are
thin wrappers around it), so calling it directly avoids an unnecessary internal network hop and
avoids needing a bearer token to talk to itself.

**Stateless by design, not by accident**: ``run_tick`` takes no ``state`` argument and keeps none
between calls -- every "when did X last run" check is derived FRESH each call from the real
``jobs`` table's own existing audit trail (``_fetch_last_successful_run_at``), not a separate
in-memory or persisted state store. This is deliberate, not a missing feature: this repo has a
standing precedent against duplicating state that's already derivable from existing data (see
e.g. how price-momentum tracking reuses real ``player_gameweek_stats``/transfer counts rather than
its own tracking table). It also means ``run_tick`` is trivially safe to call from two different
places with identical, correct behavior:

1. ``scripts/scheduler.py``'s own long-lived local loop (``while True: run_tick(); sleep()``) --
   for a local Docker Compose deployment where a persistent background process can run.
2. ``apps.api.routers.internal.trigger_scheduler_tick`` -- a real HTTP endpoint, for a deployment
   target (e.g. Render's free web-service tier) that has no persistent background-worker option on
   its free plan and can only be driven by real, external, scheduled HTTP requests (e.g.
   cron-job.org). An HTTP request/response cycle has no memory of the previous one, so a
   state-in-memory design would have been actively wrong there -- deriving from the real database
   every time is what makes both callers correct with the exact same code, no synchronization
   needed between them.

One real behavioral note versus keeping state in memory: because "last run" is derived from the
last row with ``JobStatus.SUCCEEDED`` specifically (not "last attempted"), a job that failed on its
most recent attempt is naturally retried on the very next tick rather than waiting out the full
interval again -- a deliberate improvement, not an oversight: a transient failure (a network blip
talking to the real FPL API, a momentary DB hiccup) shouldn't cost hours of staleness on top of
whatever caused it.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.jobs.service import execute_job
from packages.database.models.gameweek import Gameweek
from packages.database.models.job import Job
from packages.database.models.learning_report import LearningReport
from packages.database.models.recommendation_audit import RecommendationAudit
from packages.database.models.season import Season
from packages.database.session import SessionLocal
from packages.domain.jobs import JobStatus, JobType, TriggerType
from packages.shared.config import get_settings

logger = logging.getLogger(__name__)

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


def _fetch_last_successful_run_at(session: Session, job_type: JobType) -> datetime | None:
    """Most recent `completed_at` for a `SUCCEEDED` job of this type, or `None` if it has never
    succeeded. The real `jobs` table is already this scheduler's own complete audit trail of every
    run it has ever triggered (`requested_by="scheduler"`) -- no separate "last run" table needed,
    per this module's own docstring on why that would just be duplicated state."""
    return session.execute(
        select(Job.completed_at)
        .where(Job.job_type == job_type, Job.status == JobStatus.SUCCEEDED)
        .order_by(Job.completed_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _run_job_safely(job_type: JobType, parameters: dict | None, requested_by: str) -> None:
    """Run one job against the real database, isolated so a failure never kills the scheduler loop.

    Mirrors GameweekReviewJobRunner's own established failure-isolation precedent
    (`_maybe_recalibrate_price_signal`/the retraining-trigger step): catch broadly here
    specifically because this is a top-level scheduling boundary where ANY failure (a network
    blip talking to the real FPL API, a transient DB issue, a genuine bug in the job itself) must
    never stop future ticks (or, for the HTTP-triggered path, the calling request) from continuing.
    Each `execute_job` call gets its own fresh session so one job's failure can't leave a poisoned
    session for the next.
    """
    session = SessionLocal()
    try:
        job = execute_job(
            session,
            job_type=job_type,
            parameters=parameters,
            requested_by=requested_by,
            # LOCAL, not GITHUB_SCHEDULE or API -- this genuinely runs as a background scheduling
            # decision (whether from the local loop or the HTTP tick endpoint), not a human-driven
            # API call or a GitHub Actions run. `requested_by="scheduler"` is what actually
            # distinguishes this from a human's own manual invocation in the `jobs` table's audit
            # trail.
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


def run_tick() -> None:
    """One real scheduling pass: check what's due, run it. See this module's own docstring for why
    this is fully stateless (no argument, no return value, no memory kept between calls) -- every
    "was it time yet" check derives fresh from the real `jobs`/`learning_reports`/
    `recommendation_audits` tables' own existing state each call, so this is safe to call from
    either a long-lived loop or a single, independent HTTP request with identical correctness.
    """
    now = datetime.now(UTC)

    session = SessionLocal()
    try:
        season_id, due_event_ids = _fetch_gameweek_review_state(session)
        has_authoritative_recommendation_event_ids = (
            _fetch_authoritative_recommendation_event_ids(session, season_id, due_event_ids)
            if season_id is not None
            else set()
        )
        last_league_sync_at = _fetch_last_successful_run_at(session, JobType.LEAGUE_SYNC)
        last_data_ingestion_at = _fetch_last_successful_run_at(session, JobType.DATA_INGESTION)
    except Exception as exc:  # noqa: BLE001 - a failed check must not stop other checks this tick
        logger.warning("scheduler.tick_check_failed", extra={"error": str(exc)})
        due_event_ids = []
        has_authoritative_recommendation_event_ids = set()
        last_league_sync_at = None
        last_data_ingestion_at = None
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

    if is_interval_due(last_league_sync_at, now, LEAGUE_SYNC_INTERVAL):
        fpl_league_id = get_settings().fpl_h2h_league_id
        if fpl_league_id:
            logger.info("scheduler.triggering_league_sync", extra={"fpl_league_id": fpl_league_id})
            _run_job_safely(
                JobType.LEAGUE_SYNC, {"fpl_league_id": int(fpl_league_id)}, requested_by="scheduler"
            )
        else:
            logger.info("scheduler.league_sync_skipped_no_league_configured")

    if is_interval_due(last_data_ingestion_at, now, DATA_INGESTION_INTERVAL):
        logger.info("scheduler.triggering_data_ingestion")
        _run_job_safely(JobType.DATA_INGESTION, None, requested_by="scheduler")
