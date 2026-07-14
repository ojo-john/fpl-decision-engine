"""LEAGUE_SYNC job: sync an H2H league's standings, matches, and rival gameweek picks.

Milestone 5 Layer 1 (H2H Strategy foundations). Fetches
``/leagues-h2h/{id}/standings/`` and ``/leagues-h2h-matches/league/{id}/``
(see ``packages.fpl_client.client.FPLClient``'s new H2H methods), paginating
each until ``has_next`` is false, and upserts ``League``/``LeagueEntry``/
``H2HMatch`` rows (current-state, upsert-in-place — see each model's own
docstring). For every standings entry except ``exclude_fpl_manager_id`` (the
calling user's own manager id, if provided — see the "self exclusion" note
below), fetches that rival's picks for the most recent *finished* gameweek
and persists one insert-only ``RivalGameweekSnapshot`` audit row per rival,
regardless of outcome.

Self-exclusion is optional, not required: if ``exclude_fpl_manager_id`` is
omitted, every entry in the league is synced as a "rival", including the
calling user themselves. This is documented, intentional behavior (not a
bug) — LEAGUE_SYNC has no other way to know which entry is "self" beyond
being told, and syncing everyone is a harmless superset.

Run-row discipline (mirrors ``SquadOptimizationJobRunner``/
``TransferOptimizationJobRunner``): a ``LeagueSyncRun`` row is created early
— right after the ``League`` row and its ``LeagueEntry`` rows are upserted,
before the failure-prone remaining work (matches fetch, per-rival picks
loop) — so a crash mid-sync still leaves a terminal (``FAILED``) row behind
via ``_finalize_failed``. If the *standings* fetch itself fails (e.g. a bad
``fpl_league_id``), that happens before any row exists at all — deliberately
no ``LeagueSyncRun`` row is created in that case, mirroring
``app.optimization.gate``'s "no row at all when we refuse outright" and
``TransferOptimizationJobRunner``'s "no row for a pre-row-creation
``RulesBlockedError``" precedent.

Per-rival failure isolation: ``_sync_one_rival`` never raises — any failure
(network, resolution, persistence) for one rival is caught, logged, and
turned into a minimal ``FETCH_FAILED`` snapshot rather than aborting the
whole run. Each snapshot is committed individually as soon as it's built, so
one rival's failure can never roll back another rival's already-persisted
snapshot (see ``packages.domain.h2h.PicksAvailability``'s own docstring for
why a clean HTTP 404 is the one signal this system can confidently call
"unavailable", with everything else — timeout, connection error, 5xx,
invalid JSON, or an unexpected bug in resolution/persistence — bucketed as
``FETCH_FAILED``).

Player resolution reuses ``app.jobs.handlers.manager_sync._resolve_players``
verbatim (imported directly, matching this codebase's existing precedent of
importing ``app.ingestion.normalization._bulk_upsert`` across modules
rather than duplicating it) — a rival's squad element resolution is exactly
the same "resolve to this season's ``Player.fpl_element_id``, report
unresolved ids, never block on it" logic ``MANAGER_SYNC`` already
established.

Milestone 5 Layer 2 (H2H Strategy: rival-state derivation/prediction) is
appended to this same handler rather than built as its own job type — after
every rival's ``RivalGameweekSnapshot`` row is persisted for this run (the
block above), ``_compute_and_upsert_rival_profile`` runs for every rival in
the league (regardless of *this run's* picks-fetch outcome — it reuses
whatever ``RivalGameweekSnapshot``/``/history/`` data already exists),
computing the pure ``app.h2h.rival_state.compute_rival_state`` output and
upserting one current-state ``RivalProfile`` row per ``(season_id,
fpl_manager_id)``. Mirrors ``_sync_one_rival``'s per-rival failure
isolation discipline exactly: one rival's ``/history/`` fetch failing (or
any other unexpected failure while computing their profile) must never
abort the whole run — it degrades only that rival's own profile.
"""

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.h2h.rival_state import (
    CaptainObservation,
    RivalStateInputs,
    TransferTargetCandidate,
    compute_rival_state,
)
from app.ingestion.normalization import _bulk_upsert
from app.jobs.handlers.manager_sync import _resolve_players
from app.jobs.registry import register_handler
from app.jobs.runner import JobContext, JobResult, JobRunner
from app.optimization.gameweek_resolution import resolve_most_recent_finished_gameweek_event
from app.optimization.projection import project_player
from app.transfers.rules import load_current_rules_config_id, load_transfer_rules
from packages.database.models.h2h_match import H2HMatch
from packages.database.models.league import League
from packages.database.models.league_entry import LeagueEntry
from packages.database.models.league_sync_run import LeagueSyncRun
from packages.database.models.player import Player
from packages.database.models.position import Position
from packages.database.models.rival_gameweek_snapshot import RivalGameweekSnapshot
from packages.database.models.rival_profile import RivalProfile
from packages.database.models.rules_config import RulesConfig
from packages.database.models.season import Season
from packages.database.session import SessionLocal
from packages.domain.h2h import LeagueSyncRunStatus, LeagueType, PicksAvailability
from packages.domain.jobs import JobStatus, JobType
from packages.fpl_client import FetchResult, FPLClient

logger = logging.getLogger(__name__)

_TOP_TRANSFER_TARGET_CANDIDATES = 5


class _LeagueMatchesFetchFailedError(Exception):
    """Raised internally when the H2H matches fetch fails after the run row already exists.

    Caught by the same outer handler that finalizes ``LeagueSyncRun`` to
    ``FAILED`` on any other post-row-creation failure — this just gives that
    one specific failure mode a named type instead of a bare ``RuntimeError``.
    """


def _resolve_current_season(session: Session) -> Season | None:
    return session.execute(select(Season).where(Season.is_current.is_(True))).scalar_one_or_none()


def classify_picks_availability(fetch_result: FetchResult) -> PicksAvailability:
    """Classify a completed (non-raising) picks fetch that had no usable body.

    Only ever called when ``fetch_result.raw_body is None``. A clean HTTP 404
    is the one signal this system can confidently call "unavailable" (see
    ``PicksAvailability``'s own docstring); everything else — timeout,
    connection error, 5xx, invalid JSON — is bucketed as ``FETCH_FAILED``.
    """
    if fetch_result.http_status == 404:
        return PicksAvailability.UNAVAILABLE_PRIVATE_OR_NOT_RELEASED
    return PicksAvailability.FETCH_FAILED


def _fetch_all_standings(client: FPLClient, fpl_league_id: int) -> tuple[dict, list[dict]] | None:
    """Fetch every page of H2H league standings, accumulating all ``results``.

    Returns ``(league_json, standings_results)``, or ``None`` if any page
    fetch fails (no usable body) — the caller treats that as a league-level
    failure.
    """
    all_results: list[dict] = []
    league_json: dict | None = None
    page = 1
    while True:
        result = client.fetch_h2h_league_standings(fpl_league_id, page=page)
        if result.raw_body is None:
            return None
        body = result.raw_body
        assert isinstance(body, dict)  # standings always returns a JSON object
        if league_json is None:
            league_json = body["league"]
        standings = body["standings"]
        all_results.extend(standings["results"])
        if not standings.get("has_next"):
            break
        page += 1
    assert league_json is not None
    return league_json, all_results


def _fetch_all_matches(client: FPLClient, fpl_league_id: int) -> list[dict] | None:
    """Fetch every page of H2H league matches, accumulating all ``results``.

    Returns ``None`` if any page fetch fails (no usable body).
    """
    all_results: list[dict] = []
    page = 1
    while True:
        result = client.fetch_h2h_matches(fpl_league_id, page=page)
        if result.raw_body is None:
            return None
        body = result.raw_body
        assert isinstance(body, dict)  # matches always returns a JSON object
        all_results.extend(body["results"])
        if not body.get("has_next"):
            break
        page += 1
    return all_results


def _upsert_league(session: Session, fpl_league_id: int, league_json: dict, now: datetime) -> League:
    """Upsert the one ``League`` row for this league, then return it (freshly queried).

    ``_bulk_upsert``'s ``INSERT ... ON CONFLICT`` never populates the ORM
    identity map, so the row must be re-queried by its natural key afterward
    to get a usable ``League`` object (same reason every other normalization
    caller re-queries FK maps after a bulk upsert).
    """
    existing_keys = {(fpl_id,) for (fpl_id,) in session.execute(select(League.fpl_league_id))}
    values = [
        {
            "id": uuid.uuid4(),
            "fpl_league_id": fpl_league_id,
            "name": league_json["name"],
            # LEAGUE_SYNC only ever syncs H2H leagues -- see LeagueType's own
            # docstring for why this is our own classification, not parsed
            # from the raw response.
            "league_type": LeagueType.H2H,
            "scoring": league_json.get("scoring"),
            "admin_entry": league_json.get("admin_entry"),
            "start_event": league_json.get("start_event"),
            "ko_rounds": league_json.get("ko_rounds"),
            "closed": league_json.get("closed", False),
            "has_cup": league_json.get("has_cup", False),
            "raw_league_json": league_json,
            "last_synced_at": now,
        }
    ]
    _bulk_upsert(
        session,
        League,
        values,
        index_elements=["fpl_league_id"],
        existing_keys=existing_keys,
        key_fields=["fpl_league_id"],
    )
    session.flush()
    return session.execute(select(League).where(League.fpl_league_id == fpl_league_id)).scalar_one()


def _upsert_league_entries(
    session: Session, league_id: uuid.UUID, standings_results: list[dict], now: datetime
) -> None:
    if not standings_results:
        return
    existing_keys = {
        (row_league_id, fpl_manager_id)
        for (row_league_id, fpl_manager_id) in session.execute(
            select(LeagueEntry.league_id, LeagueEntry.fpl_manager_id).where(
                LeagueEntry.league_id == league_id
            )
        )
    }
    values = [
        {
            "id": uuid.uuid4(),
            "league_id": league_id,
            "fpl_manager_id": row["entry"],
            "player_name": row.get("player_name"),
            "entry_name": row.get("entry_name"),
            "rank": row.get("rank"),
            "last_rank": row.get("last_rank"),
            "matches_played": row.get("matches_played"),
            "matches_won": row.get("matches_won"),
            "matches_drawn": row.get("matches_drawn"),
            "matches_lost": row.get("matches_lost"),
            "points_for": row.get("points_for"),
            "total": row.get("total"),
            "division": row.get("division"),
            "last_synced_at": now,
        }
        for row in standings_results
    ]
    _bulk_upsert(
        session,
        LeagueEntry,
        values,
        index_elements=["league_id", "fpl_manager_id"],
        existing_keys=existing_keys,
        key_fields=["league_id", "fpl_manager_id"],
    )


def _upsert_matches(session: Session, league_id: uuid.UUID, matches: list[dict], now: datetime) -> None:
    if not matches:
        return
    existing_keys = {(fpl_match_id,) for (fpl_match_id,) in session.execute(select(H2HMatch.fpl_match_id))}
    values = [
        {
            "id": uuid.uuid4(),
            "fpl_match_id": row["id"],
            "league_id": league_id,
            "event": row["event"],
            "entry_1_id": row["entry_1_entry"],
            "entry_2_id": row["entry_2_entry"],
            "entry_1_points": row.get("entry_1_points"),
            "entry_2_points": row.get("entry_2_points"),
            "entry_1_win": row.get("entry_1_win"),
            "entry_1_draw": row.get("entry_1_draw"),
            "entry_1_loss": row.get("entry_1_loss"),
            "entry_2_win": row.get("entry_2_win"),
            "entry_2_draw": row.get("entry_2_draw"),
            "entry_2_loss": row.get("entry_2_loss"),
            "winner": row.get("winner"),
            "is_knockout": row.get("is_knockout", False),
            "knockout_name": row.get("knockout_name"),
            "is_bye": row.get("is_bye", False),
            "seed_value": row.get("seed_value"),
            "raw_match_json": row,
            "last_synced_at": now,
        }
        for row in matches
    ]
    _bulk_upsert(
        session,
        H2HMatch,
        values,
        index_elements=["fpl_match_id"],
        existing_keys=existing_keys,
        key_fields=["fpl_match_id"],
    )


def _minimal_snapshot(
    fpl_manager_id: int,
    fpl_event_id: int,
    as_of: datetime,
    availability: PicksAvailability,
    http_status: int | None,
) -> RivalGameweekSnapshot:
    return RivalGameweekSnapshot(
        fpl_manager_id=fpl_manager_id,
        fpl_event_id=fpl_event_id,
        as_of=as_of,
        picks_availability=availability,
        http_status=http_status,
    )


def _sync_one_rival(
    session: Session,
    client: FPLClient,
    season_id: uuid.UUID,
    fpl_manager_id: int,
    fpl_event_id: int,
    as_of: datetime,
) -> RivalGameweekSnapshot:
    """Fetch + build one rival's picks snapshot. Never raises.

    This is the one deliberate broad-except boundary in this handler: any
    failure while syncing a single rival (network, resolution, an unexpected
    bug) must never abort the rest of the league sync, matching "error
    granularity matches work granularity" for independent batch items. The
    caller is responsible for ``session.add``+``session.commit``ing the
    returned row immediately, so one rival's failure can never roll back
    another rival's already-persisted snapshot.
    """
    try:
        picks_result = client.fetch_manager_picks(fpl_manager_id, fpl_event_id)

        if picks_result.raw_body is None:
            availability = classify_picks_availability(picks_result)
            if availability == PicksAvailability.FETCH_FAILED:
                logger.warning(
                    "league_sync.rival_picks_fetch_failed",
                    extra={
                        "fpl_manager_id": fpl_manager_id,
                        "fpl_event_id": fpl_event_id,
                        "http_status": picks_result.http_status,
                        "error": picks_result.error_message,
                    },
                )
            return _minimal_snapshot(
                fpl_manager_id, fpl_event_id, as_of, availability, picks_result.http_status
            )

        picks_body = picks_result.raw_body
        assert isinstance(picks_body, dict)  # picks always returns a JSON object
        squad_picks = picks_body.get("picks", [])
        owned_element_ids = {p["element"] for p in squad_picks}
        player_by_element_id, unresolved_element_ids = _resolve_players(session, season_id, owned_element_ids)

        captain_element_id = next((p["element"] for p in squad_picks if p.get("is_captain")), None)
        vice_captain_element_id = next((p["element"] for p in squad_picks if p.get("is_vice_captain")), None)
        entry_history = picks_body.get("entry_history") or {}

        return RivalGameweekSnapshot(
            fpl_manager_id=fpl_manager_id,
            fpl_event_id=fpl_event_id,
            as_of=as_of,
            picks_availability=PicksAvailability.AVAILABLE,
            http_status=picks_result.http_status,
            active_chip=picks_body.get("active_chip"),
            captain_element_id=captain_element_id,
            vice_captain_element_id=vice_captain_element_id,
            squad_player_ids=[str(p.id) for p in player_by_element_id.values()],
            unresolved_fpl_element_ids_json=unresolved_element_ids,
            event_points=entry_history.get("points"),
            event_transfers=entry_history.get("event_transfers"),
            event_transfers_cost=entry_history.get("event_transfers_cost"),
            overall_rank=entry_history.get("overall_rank"),
            raw_picks_json=picks_body,
        )
    except Exception as exc:  # noqa: BLE001 - one rival's failure must never abort the whole run
        session.rollback()
        logger.warning(
            "league_sync.rival_sync_raised",
            extra={"fpl_manager_id": fpl_manager_id, "fpl_event_id": fpl_event_id, "error": str(exc)},
        )
        return _minimal_snapshot(fpl_manager_id, fpl_event_id, as_of, PicksAvailability.FETCH_FAILED, None)


def _load_chip_inventory(session: Session, rules_config_id: uuid.UUID) -> list[str]:
    """Distinct chip names available this season, from the current rules_config's raw chips[].

    De-duplicated (a chip typically appears twice in FPL's raw ``chips[]`` --
    once per season half -- ``app.h2h.rival_state`` also de-duplicates this
    same inventory defensively, so passing a pre-deduplicated list here is a
    belt-and-braces choice, not a requirement).
    """
    raw_chips = session.execute(
        select(RulesConfig.raw_chips_json).where(RulesConfig.id == rules_config_id)
    ).scalar_one()
    return list(dict.fromkeys(chip["name"] for chip in raw_chips))


def _load_population_mean_selected_by_percent(session: Session, season_id: uuid.UUID) -> float | None:
    """Season-wide average ownership %, across non-quarantined players -- the population signal.

    ``None`` if no players are ingested yet (e.g. a brand-new season with no
    completed DATA_INGESTION run) -- callers must treat this as "population
    comparison unavailable", not an error.
    """
    mean_value = session.execute(
        select(func.avg(Player.selected_by_percent)).where(
            Player.season_id == season_id, Player.is_quarantined.is_(False)
        )
    ).scalar_one_or_none()
    return float(mean_value) if mean_value is not None else None


def _load_latest_snapshot_per_event(session: Session, fpl_manager_id: int) -> list[RivalGameweekSnapshot]:
    """One snapshot row per distinct ``fpl_event_id`` for this manager -- the most recently created attempt.

    ``RivalGameweekSnapshot`` is insert-only (see its own docstring): repeated
    ``LEAGUE_SYNC`` runs targeting the same still-most-recent-finished
    gameweek can leave multiple attempt rows for the same ``(fpl_manager_id,
    fpl_event_id)``. Layer 2's tendency/captain-frequency statistics must
    count each real gameweek exactly once (its latest attempt), or a
    re-synced-but-unchanged gameweek would silently inflate that gameweek's
    weight in the computed distribution.
    """
    rows = (
        session.execute(
            select(RivalGameweekSnapshot)
            .where(RivalGameweekSnapshot.fpl_manager_id == fpl_manager_id)
            .order_by(RivalGameweekSnapshot.fpl_event_id.asc(), RivalGameweekSnapshot.created_at.desc())
        )
        .scalars()
        .all()
    )
    latest_by_event: dict[int, RivalGameweekSnapshot] = {}
    for row in rows:
        latest_by_event.setdefault(row.fpl_event_id, row)
    return sorted(latest_by_event.values(), key=lambda r: r.fpl_event_id)


def _build_transfer_target_candidates(
    session: Session, season_id: uuid.UUID, squad_player_ids: list[str]
) -> list[TransferTargetCandidate]:
    """Heuristic replacement-player candidates for a rival's single weakest squad position.

    Deliberately simple, cheap, advisory-only: identifies the one position
    where the rival's current squad's average projected points
    (``app.optimization.projection.project_player`` — the plain ep_next/
    points_per_game heuristic, not the champion-aware path, which needs a
    resolved target-gameweek id and loaded champion context that doesn't
    reliably exist in this per-rival-per-league-sync setting) is lowest, then
    ranks unowned same-position players by projected points. This is a
    heuristic signal, not a claim of precision — see
    ``RivalStateResult.predicted_likely_transfer_targets``'s own framing.
    """
    squad_uuids = [uuid.UUID(pid) for pid in squad_player_ids]
    squad_players = session.execute(select(Player).where(Player.id.in_(squad_uuids))).scalars().all()
    if not squad_players:
        return []

    projected_points_by_position: dict[uuid.UUID, list[float]] = {}
    for player in squad_players:
        if player.position_id is None:
            continue
        projection = project_player(player)
        projected_points_by_position.setdefault(player.position_id, []).append(projection.projected_points)

    if not projected_points_by_position:
        return []

    weakest_position_id = min(
        projected_points_by_position,
        key=lambda position_id: (
            sum(projected_points_by_position[position_id]) / len(projected_points_by_position[position_id])
        ),
    )
    position = session.get(Position, weakest_position_id)
    position_short_name = position.short_name if position is not None else None

    owned_ids = {player.id for player in squad_players}
    candidates = (
        session.execute(
            select(Player).where(
                Player.season_id == season_id,
                Player.position_id == weakest_position_id,
                Player.is_quarantined.is_(False),
            )
        )
        .scalars()
        .all()
    )

    scored: list[tuple[float, TransferTargetCandidate]] = []
    for candidate in candidates:
        if candidate.id in owned_ids:
            continue
        projection = project_player(candidate)
        if not projection.is_eligible:
            continue
        scored.append(
            (
                projection.projected_points,
                TransferTargetCandidate(
                    player_id=str(candidate.id),
                    web_name=candidate.web_name,
                    position=position_short_name,
                    projected_points=projection.projected_points,
                    selected_by_percent=(
                        float(candidate.selected_by_percent)
                        if candidate.selected_by_percent is not None
                        else None
                    ),
                ),
            )
        )

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [candidate for _, candidate in scored[:_TOP_TRANSFER_TARGET_CANDIDATES]]


def _compute_and_upsert_rival_profile(
    session: Session,
    client: FPLClient,
    season_id: uuid.UUID,
    league_id: uuid.UUID,
    fpl_manager_id: int,
    chip_inventory: list[str],
    free_transfers_cap: int,
    population_mean_selected_by_percent: float | None,
    now: datetime,
) -> None:
    """Compute one rival's full Layer 2 state and upsert their ``RivalProfile`` row. Never raises.

    This is the one deliberate broad-except boundary for Layer 2 (mirrors
    ``_sync_one_rival``'s own docstring): any failure while computing one
    rival's profile (network, an unexpected bug in candidate-ranking, a
    query failure) must never abort the rest of the league sync. A rival's
    own ``/history/`` fetch failing specifically degrades ONLY that rival's
    profile to ``history_available=False`` (maximally-low-confidence,
    mostly-unknown — see ``app.h2h.rival_state``), rather than skipping the
    row entirely; anything else unexpected skips upserting this rival's
    profile for this run only (any prior profile row is left untouched).
    """
    try:
        history_available = False
        history_current_rows: list[dict] = []
        history_chip_rows: list[dict] = []
        try:
            history_result = client.fetch_manager_history(fpl_manager_id)
            if history_result.raw_body is not None:
                assert isinstance(history_result.raw_body, dict)  # history always returns a JSON object
                history_current_rows = history_result.raw_body.get("current", [])
                history_chip_rows = history_result.raw_body.get("chips", [])
                history_available = True
            else:
                logger.warning(
                    "league_sync.rival_history_fetch_failed",
                    extra={
                        "fpl_manager_id": fpl_manager_id,
                        "http_status": history_result.http_status,
                        "error": history_result.error_message,
                    },
                )
        except Exception as exc:  # noqa: BLE001 - a history-fetch failure degrades, never aborts
            logger.warning(
                "league_sync.rival_history_fetch_raised",
                extra={"fpl_manager_id": fpl_manager_id, "error": str(exc)},
            )

        latest_snapshots = _load_latest_snapshot_per_event(session, fpl_manager_id)
        available_snapshots = [
            s for s in latest_snapshots if s.picks_availability == PicksAvailability.AVAILABLE
        ]
        captain_observations = [
            CaptainObservation(fpl_event_id=s.fpl_event_id, captain_element_id=s.captain_element_id)
            for s in available_snapshots
        ]
        last_known_snapshot = available_snapshots[-1] if available_snapshots else None

        squad_selected_by_percent: list[float] | None = None
        transfer_target_candidates: list[TransferTargetCandidate] = []
        if last_known_snapshot is not None and last_known_snapshot.squad_player_ids:
            squad_uuids = [uuid.UUID(pid) for pid in last_known_snapshot.squad_player_ids]
            squad_players = session.execute(select(Player).where(Player.id.in_(squad_uuids))).scalars().all()
            ownerships = [
                float(p.selected_by_percent) for p in squad_players if p.selected_by_percent is not None
            ]
            squad_selected_by_percent = ownerships or None
            transfer_target_candidates = _build_transfer_target_candidates(
                session, season_id, last_known_snapshot.squad_player_ids
            )

        inputs = RivalStateInputs(
            fpl_manager_id=fpl_manager_id,
            chip_inventory=chip_inventory,
            free_transfers_cap=free_transfers_cap,
            history_available=history_available,
            history_current_rows=history_current_rows,
            history_chip_rows=history_chip_rows,
            captain_observations=captain_observations,
            last_known_snapshot_id=last_known_snapshot.id if last_known_snapshot is not None else None,
            squad_selected_by_percent=squad_selected_by_percent,
            population_mean_selected_by_percent=population_mean_selected_by_percent,
            transfer_target_candidates=transfer_target_candidates,
        )
        result = compute_rival_state(inputs)

        existing_keys = {
            (row_season_id, row_manager_id)
            for (row_season_id, row_manager_id) in session.execute(
                select(RivalProfile.season_id, RivalProfile.fpl_manager_id).where(
                    RivalProfile.season_id == season_id, RivalProfile.fpl_manager_id == fpl_manager_id
                )
            )
        }
        values = [
            {
                "id": uuid.uuid4(),
                "season_id": season_id,
                "fpl_manager_id": fpl_manager_id,
                "league_id": league_id,
                "last_known_snapshot_id": result.last_known_snapshot_id,
                "derived_free_transfers_min": result.derived_free_transfers_min,
                "derived_free_transfers_max": result.derived_free_transfers_max,
                "derived_free_transfers_confidence": result.derived_free_transfers_confidence,
                "derived_likely_remaining_chips_json": result.derived_likely_remaining_chips,
                "predicted_no_transfer_probability": result.predicted_no_transfer_probability,
                "predicted_one_transfer_probability": result.predicted_one_transfer_probability,
                "predicted_multi_transfer_probability": result.predicted_multi_transfer_probability,
                "predicted_hit_probability": result.predicted_hit_probability,
                "predicted_captain_probabilities_json": result.predicted_captain_probabilities,
                "predicted_likely_transfer_targets_json": result.predicted_likely_transfer_targets,
                "tendency_features_json": result.tendency_features,
                "unknowns_json": result.unknowns,
                "evidence_json": result.evidence,
                "overall_confidence": result.overall_confidence,
                "computed_at": now,
            }
        ]
        _bulk_upsert(
            session,
            RivalProfile,
            values,
            index_elements=["season_id", "fpl_manager_id"],
            existing_keys=existing_keys,
            key_fields=["season_id", "fpl_manager_id"],
        )
        session.commit()
    except Exception as exc:  # noqa: BLE001 - one rival's profile failure must never abort the whole run
        session.rollback()
        logger.warning(
            "league_sync.rival_profile_computation_failed",
            extra={"fpl_manager_id": fpl_manager_id, "error": str(exc)},
        )


@register_handler(JobType.LEAGUE_SYNC)
class LeagueSyncJobRunner(JobRunner):
    """Syncs an H2H league's standings/matches, plus each rival's latest finished-gameweek picks."""

    def run(self, ctx: JobContext) -> JobResult:
        session = SessionLocal()
        client = FPLClient()
        run_row: LeagueSyncRun | None = None
        try:
            raw_league_id = (ctx.parameters or {}).get("fpl_league_id")
            if raw_league_id is None:
                return JobResult(
                    status=JobStatus.FAILED,
                    output=None,
                    error_code="MISSING_LEAGUE_ID",
                    error_message="parameters.fpl_league_id is required.",
                )
            try:
                fpl_league_id = int(raw_league_id)
            except (TypeError, ValueError):
                return JobResult(
                    status=JobStatus.FAILED,
                    output=None,
                    error_code="INVALID_LEAGUE_ID",
                    error_message="parameters.fpl_league_id must be an integer.",
                )

            raw_exclude_id = (ctx.parameters or {}).get("exclude_fpl_manager_id")
            exclude_fpl_manager_id = int(raw_exclude_id) if raw_exclude_id is not None else None

            season = _resolve_current_season(session)
            if season is None:
                return JobResult(
                    status=JobStatus.FAILED,
                    output=None,
                    error_code="NO_CURRENT_SEASON",
                    error_message="No current season found.",
                )

            # League-level resolution: a failure here happens before any row
            # exists at all, so deliberately no LeagueSyncRun row is created
            # (mirrors app.optimization.gate's "no row at all when we refuse
            # outright").
            standings = _fetch_all_standings(client, fpl_league_id)
            if standings is None:
                return JobResult(
                    status=JobStatus.FAILED,
                    output=None,
                    error_code="LEAGUE_STANDINGS_FETCH_FAILED",
                    error_message=f"Could not fetch H2H league standings for league {fpl_league_id}.",
                )
            league_json, standings_results = standings

            try:
                now = datetime.now(UTC)
                league = _upsert_league(session, fpl_league_id, league_json, now)
                _upsert_league_entries(session, league.id, standings_results, now)
                session.commit()

                run_row = LeagueSyncRun(
                    job_id=uuid.UUID(ctx.job_id),
                    league_id=league.id,
                    season_id=season.id,
                    run_status=LeagueSyncRunStatus.RUNNING,
                    started_at=now,
                )
                session.add(run_row)
                session.commit()
                session.refresh(run_row)

                matches = _fetch_all_matches(client, fpl_league_id)
                if matches is None:
                    raise _LeagueMatchesFetchFailedError(
                        f"Could not fetch H2H matches for league {fpl_league_id}."
                    )
                _upsert_matches(session, league.id, matches, now)
                session.commit()

                fpl_event_id = resolve_most_recent_finished_gameweek_event(session, season.id)

                rivals_synced = 0
                rivals_unavailable = 0
                if fpl_event_id is not None:
                    for entry_row in standings_results:
                        fpl_manager_id = entry_row["entry"]
                        if exclude_fpl_manager_id is not None and fpl_manager_id == exclude_fpl_manager_id:
                            continue
                        snapshot = _sync_one_rival(
                            session, client, season.id, fpl_manager_id, fpl_event_id, now
                        )
                        session.add(snapshot)
                        session.commit()
                        if snapshot.picks_availability == PicksAvailability.AVAILABLE:
                            rivals_synced += 1
                        else:
                            rivals_unavailable += 1

                # Milestone 5 Layer 2: compute + upsert one RivalProfile per
                # rival, for every rival in the league regardless of this
                # run's picks-fetch outcome (see _compute_and_upsert_rival_profile's
                # own docstring) -- independent of fpl_event_id/the picks
                # loop above, since /history/ data exists whether or not a
                # gameweek has finished yet this season.
                rules_config_id = load_current_rules_config_id(session, season.id)
                transfer_rules = load_transfer_rules(session, rules_config_id)
                chip_inventory = _load_chip_inventory(session, rules_config_id)
                population_mean_selected_by_percent = _load_population_mean_selected_by_percent(
                    session, season.id
                )

                rival_profiles_computed = 0
                for entry_row in standings_results:
                    fpl_manager_id = entry_row["entry"]
                    if exclude_fpl_manager_id is not None and fpl_manager_id == exclude_fpl_manager_id:
                        continue
                    _compute_and_upsert_rival_profile(
                        session,
                        client,
                        season.id,
                        league.id,
                        fpl_manager_id,
                        chip_inventory,
                        transfer_rules.free_transfers_cap,
                        population_mean_selected_by_percent,
                        now,
                    )
                    rival_profiles_computed += 1

                run_row.run_status = (
                    LeagueSyncRunStatus.SUCCEEDED
                    if rivals_unavailable == 0
                    else LeagueSyncRunStatus.SUCCEEDED_WITH_WARNINGS
                )
                run_row.rivals_synced_count = rivals_synced
                run_row.rivals_unavailable_count = rivals_unavailable
                run_row.completed_at = datetime.now(UTC)
                session.commit()

                return JobResult(
                    status=JobStatus.SUCCEEDED,
                    output={
                        "league_sync_run_id": str(run_row.id),
                        "league_id": str(league.id),
                        "rivals_synced_count": rivals_synced,
                        "rivals_unavailable_count": rivals_unavailable,
                        "rival_profiles_computed_count": rival_profiles_computed,
                        "run_status": run_row.run_status.value,
                    },
                    error_code=None,
                    error_message=None,
                )
            except Exception as exc:  # noqa: BLE001 - league_sync_runs row must always reach a terminal status
                session.rollback()
                self._finalize_failed(session, run_row)
                return JobResult(
                    status=JobStatus.FAILED,
                    output={"league_sync_run_id": str(run_row.id)} if run_row is not None else None,
                    error_code="LEAGUE_SYNC_FAILED",
                    error_message=str(exc),
                )
        finally:
            client.close()
            session.close()

    def _finalize_failed(self, session: Session, run_row: LeagueSyncRun | None) -> None:
        if run_row is None or run_row.id is None:
            return
        try:
            fresh = session.get(LeagueSyncRun, run_row.id)
            if fresh is not None and fresh.run_status == LeagueSyncRunStatus.RUNNING:
                fresh.run_status = LeagueSyncRunStatus.FAILED
                fresh.completed_at = datetime.now(UTC)
                session.commit()
        except Exception:  # noqa: BLE001 - best-effort finalization only
            session.rollback()
