"""Orchestrates a full DATA_INGESTION run: fetch, normalize, quality-check, report.

Enforced ordering: ``seasons`` -> ``positions``, ``teams`` -> ``players`` ->
``gameweeks`` -> ``fixtures`` -> live per-gameweek stats backfill. Players
need resolved team/position foreign keys; fixtures need resolved team/
gameweek foreign keys; live stats backfill (Milestone 7 Layer 0) needs
resolved player/fixture foreign keys, so it runs last, after fixtures.
"""

import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.data_quality import checks
from app.data_quality.models import DataQualityIssue
from app.data_quality.scoring import compute_optimization_permitted, compute_score
from app.ingestion.live_stats_normalization import (
    LIVE_EVENT_DATA_SOURCE,
    resolve_live_stats_lookups,
    upsert_live_gameweek_stats,
)
from app.ingestion.normalization import (
    NormalizationCounts,
    upsert_fixtures,
    upsert_gameweeks,
    upsert_players,
    upsert_positions,
    upsert_teams,
)
from app.ingestion.report import render_ingestion_report_markdown
from app.rules.service import sync_rules
from packages.database.models.data_quality_issue import DataQualityIssue as DataQualityIssueRow
from packages.database.models.gameweek import Gameweek
from packages.database.models.ingestion_run import IngestionRun
from packages.database.models.player_gameweek_stats import PlayerGameweekStats
from packages.database.models.position import Position
from packages.database.models.raw_api_snapshot import RawApiSnapshot
from packages.database.models.season import Season
from packages.database.models.team import Team
from packages.domain.ingestion import DataQualitySeverity, EndpointHealth, IngestionRunStatus
from packages.domain.rules import RulesStatus
from packages.fpl_client import FetchResult, FPLClient


class BootstrapFetchBlockedError(Exception):
    """Raised when bootstrap-static could not be fetched at all (no usable body)."""


def derive_season_label(bootstrap_json: dict[str, Any]) -> str:
    """Derive a season label like "2025-26" from the year of the earliest gameweek deadline."""
    deadlines = [e["deadline_time"] for e in bootstrap_json["events"] if e.get("deadline_time")]
    earliest = min(deadlines)
    year = int(earliest[:4])
    return f"{year}-{str(year + 1)[-2:]}"


def get_or_create_current_season(session: Session, bootstrap_json: dict[str, Any]) -> Season:
    """Get (or create) the Season row matching this bootstrap payload, and mark it current."""
    label = derive_season_label(bootstrap_json)
    existing = session.execute(select(Season).where(Season.label == label)).scalar_one_or_none()

    if existing is not None and existing.is_current:
        return existing

    session.execute(update(Season).where(Season.is_current.is_(True)).values(is_current=False))
    session.flush()

    if existing is None:
        existing = Season(label=label, is_current=True)
        session.add(existing)
    else:
        existing.is_current = True
    session.flush()
    return existing


def _previous_schema_signature(session: Session, endpoint: str) -> str | None:
    return session.execute(
        select(RawApiSnapshot.schema_signature)
        .where(RawApiSnapshot.endpoint == endpoint, RawApiSnapshot.schema_signature.is_not(None))
        .order_by(RawApiSnapshot.retrieved_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def persist_raw_snapshot(
    session: Session, ingestion_run_id: uuid.UUID, fetch_result: FetchResult
) -> RawApiSnapshot:
    """Persist a FetchResult as a raw_api_snapshots row, detecting schema drift against history."""
    previous_signature = _previous_schema_signature(session, fetch_result.endpoint)
    drift_detected = (
        previous_signature is not None
        and fetch_result.schema_signature is not None
        and previous_signature != fetch_result.schema_signature
    )

    row = RawApiSnapshot(
        ingestion_run_id=ingestion_run_id,
        endpoint=fetch_result.endpoint,
        request_url=fetch_result.request_url,
        request_params_json=fetch_result.request_params,
        retrieved_at=fetch_result.retrieved_at,
        http_status=fetch_result.http_status,
        attempt_count=fetch_result.attempt_count,
        latency_ms=fetch_result.latency_ms,
        health_status=fetch_result.health_status,
        checksum_sha256=fetch_result.checksum_sha256,
        schema_signature=fetch_result.schema_signature,
        schema_drift_detected=drift_detected,
        raw_body=fetch_result.raw_body,
        error_message=fetch_result.error_message,
    )
    session.add(row)
    session.flush()
    return row


def _combine_health(healths: list[EndpointHealth]) -> EndpointHealth:
    if any(h == EndpointHealth.BLOCKED for h in healths):
        return EndpointHealth.BLOCKED
    if any(h == EndpointHealth.DEGRADED for h in healths):
        return EndpointHealth.DEGRADED
    return EndpointHealth.HEALTHY


def _last_successful_completed_at(session: Session, exclude_run_id: uuid.UUID) -> datetime | None:
    return session.execute(
        select(IngestionRun.completed_at)
        .where(
            IngestionRun.id != exclude_run_id,
            IngestionRun.status.in_(
                [IngestionRunStatus.SUCCEEDED, IngestionRunStatus.SUCCEEDED_WITH_WARNINGS]
            ),
            IngestionRun.completed_at.is_not(None),
        )
        .order_by(IngestionRun.completed_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _gameweeks_needing_live_stats_backfill(
    session: Session, season_id: uuid.UUID
) -> list[tuple[uuid.UUID, int]]:
    """Return ``(gameweek_id, fpl_event_id)`` for every finished gameweek with no live-event rows yet.

    Never considers an unfinished/future gameweek (``Gameweek.finished`` is
    ``False``) — ingesting partial live data for a gameweek still in
    progress would be fabricated, not real, data. Can return multiple
    gameweeks (e.g. if DATA_INGESTION hasn't run in a while) — callers must
    loop over all of them, not just the most recent.
    """
    finished_gameweeks = session.execute(
        select(Gameweek.id, Gameweek.fpl_event_id)
        .where(Gameweek.season_id == season_id, Gameweek.finished.is_(True))
        .order_by(Gameweek.fpl_event_id)
    ).all()

    already_backfilled = set(
        session.execute(
            select(PlayerGameweekStats.gameweek_id.distinct()).where(
                PlayerGameweekStats.season_id == season_id,
                PlayerGameweekStats.data_source == LIVE_EVENT_DATA_SOURCE,
            )
        )
        .scalars()
        .all()
    )

    return [
        (gw_id, fpl_event_id) for gw_id, fpl_event_id in finished_gameweeks if gw_id not in already_backfilled
    ]


def _backfill_live_gameweek_stats(
    session: Session,
    ingestion_run_id: uuid.UUID,
    season_id: uuid.UUID,
    fpl_client: FPLClient,
) -> tuple[NormalizationCounts, list[DataQualityIssue], list[int]]:
    """Backfill live per-gameweek actual stats for every newly-finished gameweek (Milestone 7 Layer 0).

    One gameweek's fetch/normalization failure (network error, unexpected
    exception, anything) must never fail the whole DATA_INGESTION run — each
    gameweek's attempt runs inside its own SAVEPOINT
    (``session.begin_nested()``), so a failure rolls back only that
    gameweek's own uncommitted work (never the rest of this run's
    already-staged positions/teams/players/gameweeks/fixtures, nor any
    earlier gameweek in this same loop that already succeeded) and is
    recorded as a WARNING ``DataQualityIssue`` instead of raising.
    """
    total_counts = NormalizationCounts(inserted=0, updated=0, quarantined=0)
    issues: list[DataQualityIssue] = []
    backfilled_event_ids: list[int] = []

    gameweeks_to_backfill = _gameweeks_needing_live_stats_backfill(session, season_id)
    if not gameweeks_to_backfill:
        return total_counts, issues, backfilled_event_ids

    lookups = resolve_live_stats_lookups(session, season_id)

    for gameweek_id, fpl_event_id in gameweeks_to_backfill:
        savepoint = session.begin_nested()
        try:
            live_result = fpl_client.fetch_gameweek_live_stats(fpl_event_id)
            persist_raw_snapshot(session, ingestion_run_id, live_result)

            if live_result.raw_body is None:
                issues.append(
                    DataQualityIssue(
                        entity_type="endpoint",
                        entity_fpl_id=fpl_event_id,
                        severity=DataQualitySeverity.WARNING,
                        check_name="live_stats_fetch_failed",
                        message=(
                            f"gameweek {fpl_event_id}: live stats fetch failed — "
                            f"{live_result.error_message or 'no usable body'}"
                        ),
                    )
                )
                savepoint.commit()
                continue

            live_body = live_result.raw_body
            assert isinstance(live_body, dict)  # /live/ always returns a JSON object, never a bare array
            live_elements = live_body.get("elements", [])
            assert isinstance(live_elements, list)  # ...with an "elements" array
            gw_counts, gw_issues = upsert_live_gameweek_stats(
                session, season_id, gameweek_id, live_elements, lookups
            )
            savepoint.commit()

            total_counts.inserted += gw_counts.inserted
            total_counts.updated += gw_counts.updated
            issues.extend(gw_issues)
            backfilled_event_ids.append(fpl_event_id)
        except Exception as exc:  # noqa: BLE001 - one gameweek's live-stats hiccup must never fail the whole run
            savepoint.rollback()
            issues.append(
                DataQualityIssue(
                    entity_type="endpoint",
                    entity_fpl_id=fpl_event_id,
                    severity=DataQualitySeverity.WARNING,
                    check_name="live_stats_backfill_failed",
                    message=(
                        f"gameweek {fpl_event_id}: live stats backfill failed — {type(exc).__name__}: {exc}"
                    ),
                )
            )

    return total_counts, issues, backfilled_event_ids


def run_ingestion(
    session: Session, ingestion_run_id: uuid.UUID, fpl_client: FPLClient, started_at: datetime
) -> dict[str, Any]:
    """Run a full ingestion pass: fetch every endpoint, normalize, quality-check, report.

    Finalizes the ``ingestion_runs`` row (``status``, ``overall_health``,
    ``data_quality_score``, ``optimization_permitted``, ``report_markdown``,
    ``summary_json``, ``completed_at``) before returning. Raises
    ``BootstrapFetchBlockedError`` if bootstrap-static could not be fetched at
    all — callers must finalize the run as FAILED themselves in that case
    (see ``app/jobs/handlers/data_ingestion.py``).
    """
    # --- Step 2: bootstrap-static -> seasons, rules, positions, teams, players ---
    bootstrap_result = fpl_client.fetch_bootstrap_static()
    persist_raw_snapshot(session, ingestion_run_id, bootstrap_result)

    if bootstrap_result.raw_body is None:
        raise BootstrapFetchBlockedError(
            bootstrap_result.error_message or "bootstrap-static fetch failed with no usable body"
        )

    bootstrap_json = bootstrap_result.raw_body
    assert isinstance(bootstrap_json, dict)  # bootstrap-static always returns a JSON object
    season = get_or_create_current_season(session, bootstrap_json)

    rules_result = sync_rules(
        session,
        bootstrap_json=bootstrap_json,
        fetched_at=bootstrap_result.retrieved_at,
        ingestion_run_id=ingestion_run_id,
        season_id=season.id,
        endpoint_health=bootstrap_result.health_status,
    )

    positions_counts = upsert_positions(session, season.id, bootstrap_json["element_types"])
    position_id_by_fpl_id: dict[int, uuid.UUID] = {
        fpl_id: position_id
        for fpl_id, position_id in session.execute(
            select(Position.fpl_element_type_id, Position.id).where(Position.season_id == season.id)
        ).all()
    }

    teams_counts = upsert_teams(
        session, season.id, bootstrap_json["teams"], last_synced_at=bootstrap_result.retrieved_at
    )
    team_id_by_fpl_id: dict[int, uuid.UUID] = {
        fpl_id: team_id
        for fpl_id, team_id in session.execute(
            select(Team.fpl_team_id, Team.id).where(Team.season_id == season.id)
        ).all()
    }

    players_counts, player_issues = upsert_players(
        session,
        season.id,
        bootstrap_json["elements"],
        team_id_by_fpl_id=team_id_by_fpl_id,
        position_id_by_fpl_id=position_id_by_fpl_id,
        last_synced_at=bootstrap_result.retrieved_at,
    )

    # --- Step 3: fixtures -> gameweeks (from bootstrap events), fixtures ---
    fixtures_result = fpl_client.fetch_fixtures(event=None)
    persist_raw_snapshot(session, ingestion_run_id, fixtures_result)

    gameweeks_counts = upsert_gameweeks(
        session, season.id, bootstrap_json["events"], last_synced_at=bootstrap_result.retrieved_at
    )
    gameweek_id_by_fpl_id: dict[int, uuid.UUID] = {
        fpl_id: gameweek_id
        for fpl_id, gameweek_id in session.execute(
            select(Gameweek.fpl_event_id, Gameweek.id).where(Gameweek.season_id == season.id)
        ).all()
    }

    fixtures_raw = fixtures_result.raw_body or []
    assert isinstance(fixtures_raw, list)  # fixtures always returns a JSON array
    fixtures_counts, fixture_issues = upsert_fixtures(
        session,
        season.id,
        fixtures_raw,
        team_id_by_fpl_id=team_id_by_fpl_id,
        gameweek_id_by_fpl_id=gameweek_id_by_fpl_id,
        last_synced_at=fixtures_result.retrieved_at,
    )

    # --- Step 3.5: live per-gameweek actual stats backfill (Milestone 7 Layer 0) ---
    # Runs for every finished gameweek that has no `live_event` rows yet
    # (handles multiple newly-finished gameweeks needing backfill in one
    # run, e.g. after DATA_INGESTION hasn't run in a while) — never for an
    # in-progress/future gameweek. A single gameweek's failure degrades
    # gracefully (WARNING issue, loop continues) and never fails this run.
    live_stats_counts, live_stats_issues, live_stats_backfilled_event_ids = _backfill_live_gameweek_stats(
        session, ingestion_run_id, season.id, fpl_client
    )

    # --- Step 4: event-status, raw snapshot only ---
    event_status_result = fpl_client.fetch_event_status()
    persist_raw_snapshot(session, ingestion_run_id, event_status_result)

    # --- Step 5: run-level data quality checks + scoring ---
    # `core_issues` is what drives the data quality score/optimization gate —
    # deliberately EXCLUDES `live_stats_issues`. Backfilling potentially many
    # (e.g. 38) historical-but-not-yet-live-ingested gameweeks in one run can
    # legitimately raise dozens of WARNING-level issues that are completely
    # expected over a real season (e.g. "unresolved_opponent_team" for every
    # player who had a genuine mid-season transfer) — this is normal
    # historical noise, not a signal that *today's* current-gameweek data is
    # untrustworthy. Feeding it into the same flat per-issue penalty model
    # that gates whether the optimizer is allowed to run today would let a
    # one-time historical backfill accidentally block real, current-gameweek
    # optimization — exactly the opposite of what this system exists to do.
    # `live_stats_issues` is still fully persisted/reported below (and still
    # counts toward `has_notable_issues`/``SUCCEEDED_WITH_WARNINGS``) for
    # audit visibility — just never toward the numeric score or gate.
    core_issues = list(player_issues) + list(fixture_issues)
    core_issues.extend(checks.check_team_count(bootstrap_json["teams"]))
    core_issues.extend(
        checks.check_squad_composition_math(
            bootstrap_json["element_types"],
            expected_squad_size=bootstrap_json["game_config"]["rules"]["squad_squadsize"],
        )
    )
    core_issues.extend(checks.check_at_most_one_current_gameweek(bootstrap_json["events"]))

    last_successful_at = _last_successful_completed_at(session, exclude_run_id=ingestion_run_id)
    core_issues.extend(checks.check_stale_ingestion(last_successful_at, now=started_at))

    run_snapshots = (
        session.execute(select(RawApiSnapshot).where(RawApiSnapshot.ingestion_run_id == ingestion_run_id))
        .scalars()
        .all()
    )
    for snapshot_row in run_snapshots:
        core_issues.extend(
            checks.check_schema_drift(snapshot_row.endpoint, snapshot_row.schema_drift_detected)
        )

    all_issues = core_issues + list(live_stats_issues)

    session.bulk_save_objects(
        [
            DataQualityIssueRow(
                ingestion_run_id=ingestion_run_id,
                entity_type=issue.entity_type,
                entity_fpl_id=issue.entity_fpl_id,
                severity=issue.severity,
                check_name=issue.check_name,
                message=issue.message,
            )
            for issue in all_issues
        ]
    )
    session.flush()

    score = compute_score(core_issues)
    overall_health = _combine_health(
        [bootstrap_result.health_status, fixtures_result.health_status, event_status_result.health_status]
    )
    rules_status = RulesStatus(rules_result["rules_status"])
    optimization_permitted = compute_optimization_permitted(score, core_issues, rules_status, overall_health)

    # --- Step 6: render report, finalize the ingestion_runs row ---
    normalization_summary: dict[str, NormalizationCounts] = {
        "positions": positions_counts,
        "teams": teams_counts,
        "players": players_counts,
        "gameweeks": gameweeks_counts,
        "fixtures": fixtures_counts,
        "live_gameweek_stats": live_stats_counts,
    }
    endpoint_rows = [
        {
            "endpoint": snap.endpoint,
            "health_status": snap.health_status.value
            if hasattr(snap.health_status, "value")
            else snap.health_status,
            "http_status": snap.http_status,
            "attempt_count": snap.attempt_count,
            "latency_ms": snap.latency_ms,
            "schema_drift_detected": snap.schema_drift_detected,
        }
        for snap in run_snapshots
    ]

    completed_at = datetime.now(UTC)
    has_notable_issues = len(all_issues) > 0
    status = (
        IngestionRunStatus.SUCCEEDED_WITH_WARNINGS if has_notable_issues else IngestionRunStatus.SUCCEEDED
    )

    report_markdown = render_ingestion_report_markdown(
        ingestion_run_id=str(ingestion_run_id),
        run_type="data_ingestion",
        started_at=started_at,
        completed_at=completed_at,
        status=status.value,
        endpoint_rows=endpoint_rows,
        rules_changed=rules_result["changed"],
        rules_version_number=rules_result["version_number"],
        rules_report_markdown=rules_result["report_markdown"],
        normalization_summary=normalization_summary,
        issues=all_issues,
        data_quality_score=score,
        optimization_permitted=optimization_permitted,
    )

    run_row = session.get(IngestionRun, ingestion_run_id)
    if run_row is None:
        raise RuntimeError(f"ingestion_runs row {ingestion_run_id} not found during finalization")
    run_row.status = status
    run_row.overall_health = overall_health
    run_row.data_quality_score = Decimal(str(score))
    run_row.optimization_permitted = optimization_permitted
    run_row.report_markdown = report_markdown
    run_row.completed_at = completed_at
    run_row.summary_json = {
        "season_label": season.label,
        "rules": rules_result,
        "normalization": {k: asdict(v) for k, v in normalization_summary.items()},
        "issue_count": len(all_issues),
        "live_stats_backfilled_event_ids": live_stats_backfilled_event_ids,
    }
    session.flush()

    return {
        "ingestion_run_id": str(ingestion_run_id),
        "season_label": season.label,
        "data_quality_score": score,
        "optimization_permitted": optimization_permitted,
        "report_markdown": report_markdown,
        "rules": rules_result,
        "normalization": {k: asdict(v) for k, v in normalization_summary.items()},
        "issue_count": len(all_issues),
        "status": status.value,
        "live_stats_backfilled_event_ids": live_stats_backfilled_event_ids,
    }
