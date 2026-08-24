"""DEADLINE_REMINDER job: Milestone 9 Layer 3 -- one proactive push when a gameweek's deadline is
close, checked every scheduler tick rather than interval-gated (see app.scheduling.tick's own
docstring on why LEAGUE_SYNC/DATA_INGESTION use an interval and this doesn't need one): correctness
here comes entirely from the dedup check below, not from throttling how often this job itself runs.

No new state table for "have we already reminded for this gameweek" -- the real `jobs` table is
already this job's own complete audit trail (mirrors app.scheduling.tick's own
`_fetch_last_successful_run_at` precedent, and this repo's standing rule against duplicating state
that's already derivable from existing data): a `SUCCEEDED` `DEADLINE_REMINDER` row whose
`output_json.target_fpl_event_id` matches the resolved gameweek means it was already sent.

FPL doesn't expose whether the manager has already made their transfer pre-deadline, so this
reminder is unconditional, not conditional on inaction (see the plan's own explicit non-goal).
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.jobs.registry import register_handler
from app.jobs.runner import JobContext, JobResult, JobRunner
from app.notifications.push_sender import send_push_to_all
from app.optimization.gameweek_resolution import resolve_target_gameweek
from packages.database.models.gameweek import Gameweek
from packages.database.models.job import Job
from packages.database.models.season import Season
from packages.database.session import SessionLocal
from packages.domain.jobs import JobStatus, JobType

# How far ahead of a deadline to start reminding -- chosen to be well before FPL's own deadline
# lock, giving the manager a real window to still act, without firing so early it's easy to ignore
# and forget about again before the deadline actually arrives.
REMINDER_WINDOW = timedelta(hours=6)


def is_within_reminder_window(deadline_time: datetime, now: datetime) -> bool:
    """True once a gameweek's deadline is close enough to warrant a reminder. Pure, no DB --
    `resolve_target_gameweek` only ever returns gameweeks with `deadline_time > now` (see that
    function's own docstring), so `deadline_time - now` is always positive here; this only needs
    to check the upper bound of the window."""
    return deadline_time - now <= REMINDER_WINDOW


def _resolve_current_season_id(session: Session) -> uuid.UUID | None:
    return session.execute(select(Season.id).where(Season.is_current.is_(True))).scalar_one_or_none()


def _already_reminded(session: Session, season_id: uuid.UUID, target_fpl_event_id: int) -> bool:
    """True if a SUCCEEDED DEADLINE_REMINDER job already exists for this exact gameweek.

    Scoped by BOTH `target_season_id` and `target_fpl_event_id` -- FPL's own gameweek numbers
    (1-38) reset every season, and this system carries multiple `seasons` rows over time (past
    seasons stay archived with `is_current=False`, see e.g. HISTORICAL_DATA_INGESTION); a
    fpl_event_id-only check would let one season's GW1 reminder permanently suppress every future
    season's GW1 reminder too. Mirrors app.optimization.gate's own established
    `summary_json[...].astext ==` precedent for querying inside a JSONB column via SQLAlchemy.
    """
    existing = session.execute(
        select(Job.id).where(
            Job.job_type == JobType.DEADLINE_REMINDER,
            Job.status == JobStatus.SUCCEEDED,
            Job.output_json["target_fpl_event_id"].astext == str(target_fpl_event_id),
            Job.output_json["target_season_id"].astext == str(season_id),
        )
    ).first()
    return existing is not None


@register_handler(JobType.DEADLINE_REMINDER)
class DeadlineReminderJobRunner(JobRunner):
    """Sends at most one push per gameweek, once its deadline is within `REMINDER_WINDOW`.

    Every "nothing to do yet" case (no current season, no actionable gameweek, deadline still far
    off, already reminded for this gameweek) is a real, expected state, not a failure -- returns
    SUCCEEDED with `reminder_sent: False` and a `reason`. Only an actual failure to send (e.g. no
    VAPID key configured) reaches JobStatus.FAILED, via `send_push_to_all` raising and this job's
    lack of its own try/except -- deliberate: a failed send must NOT be recorded as a SUCCEEDED,
    already-reminded row, or the dedup check above would permanently suppress a reminder that was
    never actually delivered, and the next tick's retry would be silently lost.
    """

    def run(self, ctx: JobContext) -> JobResult:
        session = SessionLocal()
        try:
            season_id = _resolve_current_season_id(session)
            if season_id is None:
                return JobResult(
                    status=JobStatus.SUCCEEDED,
                    output={"reminder_sent": False, "reason": "NO_CURRENT_SEASON"},
                    error_code=None,
                    error_message=None,
                )

            now = datetime.now(UTC)
            target_gameweek_id, target_gameweek_status = resolve_target_gameweek(session, season_id, now)
            if target_gameweek_id is None:
                return JobResult(
                    status=JobStatus.SUCCEEDED,
                    output={"reminder_sent": False, "reason": target_gameweek_status.value},
                    error_code=None,
                    error_message=None,
                )

            gameweek = session.get(Gameweek, target_gameweek_id)
            assert gameweek is not None  # resolve_target_gameweek only ever returns a real row's id

            if not is_within_reminder_window(gameweek.deadline_time, now):
                return JobResult(
                    status=JobStatus.SUCCEEDED,
                    output={
                        "reminder_sent": False,
                        "reason": "OUTSIDE_WINDOW",
                        "target_fpl_event_id": gameweek.fpl_event_id,
                        "target_season_id": str(season_id),
                    },
                    error_code=None,
                    error_message=None,
                )

            if _already_reminded(session, season_id, gameweek.fpl_event_id):
                return JobResult(
                    status=JobStatus.SUCCEEDED,
                    output={
                        "reminder_sent": False,
                        "reason": "ALREADY_REMINDED",
                        "target_fpl_event_id": gameweek.fpl_event_id,
                        "target_season_id": str(season_id),
                    },
                    error_code=None,
                    error_message=None,
                )

            hours_remaining = round((gameweek.deadline_time - now).total_seconds() / 3600, 1)
            send_push_to_all(
                session,
                title=f"GW{gameweek.fpl_event_id} deadline in {hours_remaining}h",
                body="Make your transfers before the deadline passes.",
            )

            return JobResult(
                status=JobStatus.SUCCEEDED,
                output={
                    "reminder_sent": True,
                    "target_fpl_event_id": gameweek.fpl_event_id,
                    "target_season_id": str(season_id),
                },
                error_code=None,
                error_message=None,
            )
        finally:
            session.close()
