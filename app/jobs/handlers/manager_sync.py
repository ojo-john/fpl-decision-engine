"""MANAGER_SYNC job: sync a real FPL manager's identity and current squad state (Milestone 4 Layer 1).

Fetches ``/entry/{id}/``, ``/entry/{id}/history/``, ``/entry/{id}/event/{event}/picks/``, and
``/entry/{id}/transfers/`` (see ``packages.fpl_client.client.FPLClient``'s new manager-facing
methods), resolves each squad ``element`` to this season's ``Player`` rows via
``Player.fpl_element_id`` (within-season resolution — NOT ``fpl_code``, the
durable cross-season key; see ``app/ingestion/historical_normalization.py``'s
own docstring about that distinction), derives free transfers available
(``app.transfers.free_transfers``) and each squad player's purchase/selling
price (``app.transfers.selling_price``), and persists it all as one new
``ManagerSquadSnapshot`` row.

Unlike most handlers, this one does NOT create an early "RUNNING" row before
doing the fetch/normalization work — the entire snapshot is built fully in
memory (all 4 fetches + normalization + replay) and only ``INSERT``ed once
complete and validated. A ``ManagerSquadSnapshot`` row only ever represents a
*complete* success; there's no partial state worth persisting on a mid-work
crash, unlike a multi-hour ingestion pass — this mirrors ``FEATURE_GENERATION``'s
own precedent of skipping a dedicated run-header table when the underlying
artifact is itself sufficient audit trail.

Unresolved-player handling: a squad ``element`` that doesn't resolve to a
current-season ``Player.fpl_element_id`` (a removed/renamed player) is
excluded from ``squad_player_ids``/the price dicts entirely, recorded
verbatim in ``unresolved_fpl_element_ids_json``, and sets
``has_unresolved_players=True`` — the sync still succeeds and proceeds, it
never blocks on this.

Purchase-price fallback: when ``PurchasePriceInfo.is_estimated=True`` (the
player was never seen as an ``element_in`` transfer — held since original
squad selection), the purchase price used for the selling-price calculation
is the player's current ``now_cost`` (assumes zero unrealized profit —
conservative, never overstates sell value), and that player's internal id is
recorded in ``estimated_purchase_price_player_ids_json``.

No-squad-yet handling (added post-review — found live, pre-season): FPL's
own ``/entry/{id}/`` payload has ``current_event=None`` before a gameweek's
deadline has passed, even though the entry itself is a real, reachable 200.
Checked explicitly right after the entry fetch and refused with
``NO_SQUAD_SET_YET`` before ever attempting the picks fetch — passing
``current_event=None`` through to ``MANAGER_PICKS_URL_TEMPLATE`` would
literally request ``.../event/None/picks/``, which FPL 404s on, previously
surfacing as a generic, confusing ``MANAGER_FETCH_BLOCKED: http_status_404``
that read like a network problem rather than what it actually is.

Corrected understanding (found live, same pre-season window, after a real
user reported this firing even after genuinely saving a squad on the
official site): ``current_event=None`` is NOT proof no squad was ever set —
confirmed directly against FPL's own live API that ``/entry/{id}/event/
{event}/picks/`` returns 404 for a gameweek's picks for EVERY manager,
regardless of whether they saved a squad, until that gameweek's OWN
deadline has actually passed (a documented, intentional FPL platform
behavior — public picks data is withheld pre-deadline, almost certainly to
prevent squad-copying before the transfer window closes for everyone at
once). So this failure is the ordinary, expected, unavoidable state for
every manager for the ENTIRE pre-season window and right up to a gameweek's
own deadline — it says nothing about whether the manager has actually
picked a team, only that FPL hasn't opened that data up yet. The error
message reflects this: it does not tell the user their save failed or that
they need to do anything differently, since (from this API's perspective)
there is nothing more they *can* do before the deadline passes.
"""

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.jobs.registry import register_handler
from app.jobs.runner import JobContext, JobResult, JobRunner
from app.transfers.free_transfers import derive_free_transfers_available
from app.transfers.rules import RulesBlockedError, load_current_rules_config_id, load_transfer_rules
from app.transfers.selling_price import PurchasePriceInfo, compute_selling_price, derive_purchase_prices
from packages.database.models.gameweek import Gameweek
from packages.database.models.manager_squad_snapshot import ManagerSquadSnapshot
from packages.database.models.player import Player
from packages.database.models.season import Season
from packages.database.session import SessionLocal
from packages.domain.jobs import JobStatus, JobType
from packages.domain.transfers import ManagerSquadSnapshotSource
from packages.fpl_client import FetchResult, FPLClient

logger = logging.getLogger(__name__)


def _resolve_current_season(session: Session) -> Season | None:
    return session.execute(select(Season).where(Season.is_current.is_(True))).scalar_one_or_none()


def _resolve_players(
    session: Session, season_id: uuid.UUID, element_ids: set[int]
) -> tuple[dict[int, Player], list[int]]:
    """Resolve raw FPL element ids to this season's Player rows via ``fpl_element_id``.

    Returns ``(player_by_element_id, unresolved_element_ids)`` — any element
    id with no matching current-season ``Player`` row is reported, never
    silently dropped or treated as an error.
    """
    if not element_ids:
        return {}, []
    players = (
        session.execute(
            select(Player).where(Player.season_id == season_id, Player.fpl_element_id.in_(element_ids))
        )
        .scalars()
        .all()
    )
    player_by_element_id = {p.fpl_element_id: p for p in players}
    unresolved = sorted(element_ids - set(player_by_element_id.keys()))
    return player_by_element_id, unresolved


def _resolve_gameweek_id(session: Session, season_id: uuid.UUID, fpl_event_id: int) -> uuid.UUID | None:
    return session.execute(
        select(Gameweek.id).where(Gameweek.season_id == season_id, Gameweek.fpl_event_id == fpl_event_id)
    ).scalar_one_or_none()


def _blocked_result(step_name: str, fetch_result: FetchResult) -> JobResult:
    return JobResult(
        status=JobStatus.FAILED,
        output=None,
        error_code="MANAGER_FETCH_BLOCKED",
        error_message=fetch_result.error_message or f"{step_name} fetch failed with no usable body",
    )


@register_handler(JobType.MANAGER_SYNC)
class ManagerSyncJobRunner(JobRunner):
    """Syncs a real FPL manager's identity + squad state into a new ``ManagerSquadSnapshot`` row."""

    def run(self, ctx: JobContext) -> JobResult:
        session = SessionLocal()
        client = FPLClient()
        try:
            raw_manager_id = (ctx.parameters or {}).get("fpl_manager_id")
            if raw_manager_id is None:
                return JobResult(
                    status=JobStatus.FAILED,
                    output=None,
                    error_code="MISSING_MANAGER_ID",
                    error_message="parameters.fpl_manager_id is required.",
                )
            fpl_manager_id = int(raw_manager_id)

            season = _resolve_current_season(session)
            if season is None:
                return JobResult(
                    status=JobStatus.FAILED,
                    output=None,
                    error_code="NO_CURRENT_SEASON",
                    error_message="No current season found.",
                )

            entry = client.fetch_manager_entry(fpl_manager_id)
            if entry.raw_body is None:
                return _blocked_result("manager entry", entry)
            assert isinstance(entry.raw_body, dict)  # entry always returns a JSON object

            # FPL's own entry payload has current_event=None before a gameweek's deadline has
            # passed -- for EVERY manager, regardless of whether they've actually saved a squad
            # (confirmed live directly against FPL's own API: /entry/{id}/event/{event}/picks/
            # 404s for every manager pre-deadline, not just ones with no squad set -- see this
            # module's own docstring's "Corrected understanding" section). Every picks fetch
            # needs a real event number -- MANAGER_PICKS_URL_TEMPLATE would otherwise literally
            # request .../event/None/picks/, which FPL 404s on, surfacing as a confusing generic
            # "MANAGER_FETCH_BLOCKED: http_status_404" that looks like a network problem rather
            # than what it actually is. Caught explicitly, before ever attempting that fetch,
            # with a message that doesn't wrongly imply the user needs to do anything -- there is
            # nothing more they can do before the deadline passes.
            current_event = entry.raw_body["current_event"]
            if current_event is None:
                return JobResult(
                    status=JobStatus.FAILED,
                    output=None,
                    error_code="NO_SQUAD_SET_YET",
                    error_message=(
                        "FPL doesn't expose any manager's picks until that gameweek's own "
                        "deadline has passed -- this is expected, not an error, and applies "
                        "whether or not you've already saved a squad. Sync again once the "
                        "deadline passes."
                    ),
                )

            history = client.fetch_manager_history(fpl_manager_id)
            if history.raw_body is None:
                return _blocked_result("manager history", history)
            assert isinstance(history.raw_body, dict)  # history always returns a JSON object

            picks = client.fetch_manager_picks(fpl_manager_id, current_event)
            if picks.raw_body is None:
                return _blocked_result("manager picks", picks)
            assert isinstance(picks.raw_body, dict)  # picks always returns a JSON object

            transfers = client.fetch_manager_transfers(fpl_manager_id)
            if transfers.raw_body is None:
                return _blocked_result("manager transfers", transfers)
            assert isinstance(transfers.raw_body, list)  # transfers always returns a JSON array

            try:
                rules_config_id = load_current_rules_config_id(session, season.id)
                transfer_rules = load_transfer_rules(session, rules_config_id)
            except RulesBlockedError as exc:
                return JobResult(
                    status=JobStatus.FAILED,
                    output=None,
                    error_code="RULES_BLOCKED",
                    error_message=str(exc),
                )

            picks_body = picks.raw_body
            squad_picks = picks_body["picks"]
            owned_element_ids = {p["element"] for p in squad_picks}

            player_by_element_id, unresolved_element_ids = _resolve_players(
                session, season.id, owned_element_ids
            )
            has_unresolved_players = len(unresolved_element_ids) > 0

            purchase_prices = derive_purchase_prices(transfers.raw_body, set(player_by_element_id.keys()))

            history_current = history.raw_body.get("current", [])
            chips_used = history.raw_body.get("chips", [])
            free_transfers_available = derive_free_transfers_available(
                history_current, chips_used, transfer_rules.free_transfers_cap
            )

            squad_player_ids: list[str] = []
            purchase_price_by_player: dict[str, int] = {}
            selling_price_by_player: dict[str, int] = {}
            estimated_purchase_price_player_ids: list[str] = []

            for element_id, player in player_by_element_id.items():
                info: PurchasePriceInfo = purchase_prices[element_id]
                if info.is_estimated or info.value is None:
                    purchase_price_used = player.now_cost
                    estimated_purchase_price_player_ids.append(str(player.id))
                else:
                    purchase_price_used = info.value

                selling_price = compute_selling_price(
                    purchase_price_used, player.now_cost, transfer_rules.transfers_sell_on_fee
                )

                player_id_str = str(player.id)
                squad_player_ids.append(player_id_str)
                purchase_price_by_player[player_id_str] = purchase_price_used
                selling_price_by_player[player_id_str] = selling_price

            entry_history = picks_body.get("entry_history") or {}
            bank = entry_history.get("bank", 0)

            if history_current:
                latest = max(history_current, key=lambda r: r["event"])
                overall_rank = latest.get("overall_rank")
                total_points = latest.get("total_points")
            else:
                overall_rank = entry.raw_body.get("summary_overall_rank")
                total_points = entry.raw_body.get("summary_overall_points")

            gameweek_id = _resolve_gameweek_id(session, season.id, current_event)
            now = datetime.now(UTC)

            # Supersede any prior current=True snapshot for this manager (UPDATE, not delete) --
            # insert-only/audit-snapshot semantics, see ManagerSquadSnapshot's own docstring.
            session.execute(
                update(ManagerSquadSnapshot)
                .where(
                    ManagerSquadSnapshot.fpl_manager_id == fpl_manager_id,
                    ManagerSquadSnapshot.source == ManagerSquadSnapshotSource.MANAGER_SYNC,
                    ManagerSquadSnapshot.is_current.is_(True),
                )
                .values(is_current=False, superseded_at=now)
            )

            snapshot = ManagerSquadSnapshot(
                season_id=season.id,
                source=ManagerSquadSnapshotSource.MANAGER_SYNC,
                fpl_manager_id=fpl_manager_id,
                job_id=uuid.UUID(ctx.job_id),
                as_of_gameweek_id=gameweek_id,
                bank=bank,
                free_transfers_available=free_transfers_available,
                active_chip=picks_body.get("active_chip"),
                chips_used_json=chips_used,
                overall_rank=overall_rank,
                total_points=total_points,
                squad_player_ids=squad_player_ids,
                squad_player_purchase_prices_json=purchase_price_by_player,
                squad_player_selling_prices_json=selling_price_by_player,
                estimated_purchase_price_player_ids_json=estimated_purchase_price_player_ids,
                unresolved_fpl_element_ids_json=unresolved_element_ids,
                has_unresolved_players=has_unresolved_players,
                raw_entry_json=entry.raw_body,
                raw_history_json=history.raw_body,
                raw_picks_json=picks_body,
                raw_transfers_json=transfers.raw_body,
                is_current=True,
                superseded_at=None,
            )
            session.add(snapshot)
            session.commit()
            session.refresh(snapshot)

            return JobResult(
                status=JobStatus.SUCCEEDED,
                output={
                    "manager_squad_snapshot_id": str(snapshot.id),
                    "has_unresolved_players": has_unresolved_players,
                    "unresolved_fpl_element_ids": unresolved_element_ids,
                },
                error_code=None,
                error_message=None,
            )
        except Exception as exc:  # noqa: BLE001 - job row must always reach a terminal status
            session.rollback()
            return JobResult(
                status=JobStatus.FAILED,
                output=None,
                error_code="MANAGER_SYNC_FAILED",
                error_message=str(exc),
            )
        finally:
            client.close()
            session.close()
