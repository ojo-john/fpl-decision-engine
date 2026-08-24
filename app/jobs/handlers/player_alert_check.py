"""PLAYER_ALERT_CHECK job: Milestone 9 Layer 4 -- one push per player whose status or price
momentum materially changed since the last check.

Reuses `app.players.price_signal.classify_price_momentum` (no new price logic -- same
active-`PriceSignalCalibration`-lookup pattern `GET /players`/`GET /players/{id}` already use, see
`apps/api/routers/players.py`) and `Player.status`/`chance_of_playing_next_round`, both already
refreshed every `DATA_INGESTION` run -- no new ingestion needed. Wired into `run_tick()` right
after `DATA_INGESTION` succeeds, since it needs fresh data to diff against.

State is `packages.database.models.player_alert_state.PlayerAlertState`, one row per player,
upserted every run regardless of whether anything material changed -- "only the latest known value
matters for diffing" (see that model's own docstring), so a quiet run still refreshes the baseline
the NEXT run compares against.

Per-player failure isolation (mirrors `app.notifications.push_sender.send_push_to_all`'s own
per-subscription isolation, and this repo's standing "error granularity matches work granularity"
principle): if a push fails for one player, that player's state is deliberately left un-upserted so
the same real change is retried next run, but every OTHER player in this same batch still gets
checked, alerted, and upserted normally. Unlike `DEADLINE_REMINDER` (a single-item-per-run job,
where letting a failure raise and fail the whole job IS the correct retry-next-tick behavior), this
job processes many independent players per run, so the same underlying principle (never silently
mark an undelivered alert as delivered) has to be applied per-item here, not job-wide.
"""

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.jobs.registry import register_handler
from app.jobs.runner import JobContext, JobResult, JobRunner
from app.notifications.push_sender import send_push_to_all
from app.players.price_signal import PriceMomentum, classify_price_momentum
from packages.database.models.player import Player
from packages.database.models.player_alert_state import PlayerAlertState
from packages.database.models.price_signal_calibration import PriceSignalCalibration
from packages.database.models.season import Season
from packages.database.session import SessionLocal
from packages.domain.jobs import JobStatus, JobType

logger = logging.getLogger(__name__)

# Player.status codes that represent genuine injury/availability doubt -- mirrors
# app.optimization.projection's own _HARD_EXCLUDE_STATUSES ({"i", "s", "u"}), plus "d" (doubtful):
# a real doubt emerging (a -> d) is exactly the kind of change a manager wants to hear about, even
# though projection.py doesn't hard-exclude "d" players from the optimizer's own candidate pool.
_ALERT_STATUSES = frozenset({"i", "s", "u", "d"})

_ALERT_MOMENTUM_VALUES = frozenset({PriceMomentum.LIKELY_RISE.value, PriceMomentum.LIKELY_FALL.value})


def is_material_status_change(old_status: str | None, new_status: str | None) -> bool:
    """True if this transition touches the injury/doubt set -- newly at risk, recovered, or
    severity changed between two at-risk statuses. Pure, no DB."""
    if old_status == new_status:
        return False
    return old_status in _ALERT_STATUSES or new_status in _ALERT_STATUSES


def is_material_momentum_change(old_momentum: str | None, new_momentum: str) -> bool:
    """True if this transition touches LIKELY_RISE/LIKELY_FALL -- a momentum starting, stopping,
    or reversing. Pure, no DB."""
    if old_momentum == new_momentum:
        return False
    return old_momentum in _ALERT_MOMENTUM_VALUES or new_momentum in _ALERT_MOMENTUM_VALUES


def _describe_change(
    existing_state: PlayerAlertState, new_status: str | None, new_momentum: str, status_changed: bool
) -> str:
    parts = []
    if status_changed:
        parts.append(f"status {existing_state.status or 'unknown'} -> {new_status or 'unknown'}")
    if new_momentum != existing_state.price_momentum:
        parts.append(f"price momentum {existing_state.price_momentum or 'unknown'} -> {new_momentum}")
    return "; ".join(parts)


def _upsert_state(
    session: Session,
    existing_state: PlayerAlertState | None,
    player: Player,
    new_momentum: str,
    now: datetime,
) -> None:
    if existing_state is not None:
        existing_state.status = player.status
        existing_state.chance_of_playing_next_round = player.chance_of_playing_next_round
        existing_state.price_momentum = new_momentum
        existing_state.last_checked_at = now
    else:
        session.add(
            PlayerAlertState(
                player_id=player.id,
                status=player.status,
                chance_of_playing_next_round=player.chance_of_playing_next_round,
                price_momentum=new_momentum,
                last_checked_at=now,
            )
        )


@register_handler(JobType.PLAYER_ALERT_CHECK)
class PlayerAlertCheckJobRunner(JobRunner):
    """Diffs every current-season player's status/price-momentum against its stored
    `PlayerAlertState` row and sends one push per player with a material change.

    A player with no prior state row (first ever check) never fires an alert on that first run --
    there is no real "change" yet, only a baseline being seeded.
    """

    def run(self, ctx: JobContext) -> JobResult:
        session = SessionLocal()
        try:
            season_id = session.execute(
                select(Season.id).where(Season.is_current.is_(True))
            ).scalar_one_or_none()
            if season_id is None:
                return JobResult(
                    status=JobStatus.SUCCEEDED,
                    output={"players_checked": 0, "alerts_sent": 0, "reason": "NO_CURRENT_SEASON"},
                    error_code=None,
                    error_message=None,
                )

            total_players = session.execute(
                select(Season.total_players).where(Season.id == season_id)
            ).scalar_one()
            active_calibration = session.execute(
                select(PriceSignalCalibration).where(PriceSignalCalibration.is_active.is_(True))
            ).scalar_one_or_none()

            players = session.execute(select(Player).where(Player.season_id == season_id)).scalars().all()
            existing_states_by_player_id: dict[uuid.UUID, PlayerAlertState] = {
                state.player_id: state
                for state in session.execute(
                    select(PlayerAlertState).where(PlayerAlertState.player_id.in_([p.id for p in players]))
                ).scalars()
            }

            now = datetime.now(UTC)
            alerts_sent = 0
            for player in players:
                signal = classify_price_momentum(
                    transfers_in_event=player.transfers_in_event,
                    transfers_out_event=player.transfers_out_event,
                    selected_by_percent=player.selected_by_percent,
                    total_players=total_players,
                    ratio_threshold=(
                        active_calibration.ratio_threshold if active_calibration is not None else None
                    ),
                    absolute_floor=(
                        active_calibration.absolute_floor if active_calibration is not None else None
                    ),
                )
                new_momentum = signal.momentum.value
                existing_state = existing_states_by_player_id.get(player.id)

                material_change = existing_state is not None and (
                    is_material_status_change(existing_state.status, player.status)
                    or is_material_momentum_change(existing_state.price_momentum, new_momentum)
                )

                if material_change:
                    assert existing_state is not None  # material_change implies this, narrows for mypy
                    status_changed = is_material_status_change(existing_state.status, player.status)
                    description = _describe_change(
                        existing_state, player.status, new_momentum, status_changed
                    )
                    try:
                        send_push_to_all(
                            session, title=f"{player.web_name or 'A player'} update", body=description
                        )
                        alerts_sent += 1
                    except Exception as exc:  # noqa: BLE001 - one player's failed send must not block the batch
                        logger.warning(
                            "player_alert_check.alert_send_failed",
                            extra={"player_id": str(player.id), "error": str(exc)},
                        )
                        continue  # leave this player's state un-upserted -- retry the same diff next run

                _upsert_state(session, existing_state, player, new_momentum, now)

            session.commit()

            return JobResult(
                status=JobStatus.SUCCEEDED,
                output={"players_checked": len(players), "alerts_sent": alerts_sent},
                error_code=None,
                error_message=None,
            )
        finally:
            session.close()
